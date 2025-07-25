import os
import sys
with open(sys.argv[0]) as f:
    code = f.read() # read the code of this file ASAP, for logging
import uuid
import time
import copy
import glob
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
import torch
torch.empty(1, device="cuda", requires_grad=True).backward() # prevents a bug on some systems
from torch import Tensor, nn
import torch.nn.functional as F
import torch.distributed as dist
# use of FlexAttention contributed by @KoszarskyB
from torch.nn.attention.flex_attention import BlockMask, flex_attention
#torch._inductor.config.coordinate_descent_tuning = True # we have banned this flag for new records because it causes compilation to take 30min
#torch.set_float32_matmul_precision('high')

# -----------------------------------------------------------------------------
# Custom operators: FP8 matmul by @YouJiacheng

@torch.library.custom_op("nanogpt::mm", mutates_args=())
def mm_op(x: Tensor, w: Tensor, x_s: float, w_s: float, grad_s: float) -> tuple[Tensor, Tensor, Tensor]:
    @torch.compile
    def impl(x: Tensor, w: Tensor):
        assert x.is_contiguous() and w.is_contiguous()
        x_f8 = x.div(x_s).to(torch.float8_e4m3fn)
        w_f8 = w.div(w_s).to(torch.float8_e4m3fn)
        out = torch._scaled_mm(
            x_f8,
            w_f8.T,
            out_dtype=torch.bfloat16,
            scale_a=x.new_tensor(x_s, dtype=torch.float32),
            scale_b=x.new_tensor(w_s, dtype=torch.float32),
            use_fast_accum=True,
        )
        return out, x_f8, w_f8

    return impl(x, w)

@mm_op.register_fake
def _(x: Tensor, w: Tensor, *_):
    assert x.ndim == w.ndim == 2
    assert x.shape[1] == w.shape[1]
    assert x.device == w.device
    assert x.is_contiguous() and w.is_contiguous()
    return x @ w.T, x.to(torch.float8_e4m3fn), w.to(torch.float8_e4m3fn)

@torch.library.custom_op("nanogpt::mm_backward", mutates_args=())
def mm_backward_op(g: Tensor, x_f8: Tensor, w_f8: Tensor, x_s: float, w_s: float, grad_s: float) -> tuple[Tensor, Tensor]:
    @torch.compile
    def impl(grad: Tensor, x_f8: Tensor, w_f8: Tensor):
        assert grad.is_contiguous()
        x_inv_s = grad.new_tensor(x_s, dtype=torch.float32)
        w_inv_s = grad.new_tensor(w_s, dtype=torch.float32)
        grad_inv_s = grad.new_tensor(grad_s, dtype=torch.float32)
        grad_f8 = grad.div(grad_s).to(torch.float8_e5m2)
        grad_x = torch._scaled_mm(
            grad_f8,
            w_f8.T.contiguous().T,
            out_dtype=torch.bfloat16,
            scale_a=grad_inv_s,
            scale_b=w_inv_s,
            use_fast_accum=False,
        )
        # faster than grad_f8_t @ x_f8, for (d_out, d_in) == (50304, 768)
        grad_w = torch._scaled_mm(
            x_f8.T.contiguous(),
            grad_f8.T.contiguous().T,
            out_dtype=torch.float32,
            scale_a=x_inv_s,
            scale_b=grad_inv_s,
            use_fast_accum=False,
        ).T
        return grad_x, grad_w

    return impl(g, x_f8, w_f8)

@mm_backward_op.register_fake
def _(g: Tensor, x_f8: Tensor, w_f8: Tensor, *_):
    return x_f8.to(torch.bfloat16), w_f8.T.contiguous().T.to(torch.float32)

def backward(ctx, grad_out: Tensor, *_):
    x_f8, w_f8 = ctx.saved_tensors
    x_s, w_s, grad_s = ctx.scales
    grad_x, grad_w = torch.ops.nanogpt.mm_backward(
        grad_out, x_f8, w_f8, x_s, w_s, grad_s
    )
    return grad_x, grad_w, None, None, None

def setup_context(ctx: torch.autograd.function.FunctionCtx, inputs, output):
    *_, x_s, w_s, grad_s = inputs
    _, x_f8, w_f8 = output
    ctx.save_for_backward(x_f8, w_f8)
    ctx.scales = x_s, w_s, grad_s
    ctx.set_materialize_grads(False)

mm_op.register_autograd(backward, setup_context=setup_context)

# -----------------------------------------------------------------------------
# Muon optimizer

@torch.compile
def zeropower_via_newtonschulz5(G: Tensor, steps: int) -> Tensor:
    """
    Newton-Schulz iteration to compute the zeroth power / orthogonalization of G. We opt to use a
    quintic iteration whose coefficients are selected to maximize the slope at zero. For the purpose
    of minimizing steps, it turns out to be empirically effective to keep increasing the slope at
    zero even beyond the point where the iteration no longer converges all the way to one everywhere
    on the interval. This iteration therefore does not produce UV^T but rather something like US'V^T
    where S' is diagonal with S_{ii}' ~ Uniform(0.5, 1.5), which turns out not to hurt model
    performance at all relative to UV^T, where USV^T = G is the SVD.
    """
    assert G.ndim >= 2 # batched Muon implementation by @scottjmaddox, and put into practice in the record by @YouJiacheng
    a, b, c = (3.4445, -4.7750,  2.0315)
    X = G.bfloat16()
    if G.size(-2) > G.size(-1):
        X = X.mT

    # Ensure spectral norm is at most 1
    X = X / (X.norm(dim=(-2, -1), keepdim=True) + 1e-7)
    # Perform the NS iterations
    for _ in range(steps):
        A = X @ X.mT
        B = b * A + c * A @ A # quintic computation strategy adapted from suggestion by @jxbz, @leloykun, and @YouJiacheng
        X = a * X + B @ X
    
    if G.size(-2) > G.size(-1):
        X = X.mT
    return X

@torch.compile
def orthogonalize(M):
    """Orthogonalize matrices without sending singular values above 1."""
    abc_list = [
        # (3955/1024, -8306/1024, 5008/1024),
        # (3735/1024, -6681/1024, 3463/1024),
        # (3799/1024, -6499/1024, 3211/1024),
        # (4019/1024, -6385/1024, 2906/1024),
        # (2677/1024, -3029/1024, 1162/1024),
        # (2172/1024, -1833/1024,  682/1024)
        (4.0848, -6.8946, 2.9270),
        (3.9505, -6.3029, 2.6377),
        (3.7418, -5.5913, 2.3037),
        (2.8769, -3.1427, 1.2046),
        (2.8366, -3.0525, 1.2012),
    ]
    X = M.bfloat16()
    transpose = X.shape[-2] > X.shape[-1]
    if transpose:
        X = X.mT
    X = X / (X.norm(dim=(-2, -1), keepdim=True) + 1e-7)
    for a, b, c in abc_list:
        A = X @ X.mT
        B = b * A + c * A @ A # quintic computation strategy adapted from suggestion by @jxbz, @leloykun, and @YouJiacheng
        X = a * X + B @ X
    
    if transpose:
        X = X.mT
    return X

@torch.compile
def soft_cap(M: Tensor, alpha: float) -> Tensor:
    """Apply min(1, x) approximately to the singular values of a single matrix."""
    # Handle batched matrices by flattening batch dimensions
    orig_shape = M.shape
    if M.ndim > 2:
        M = M.reshape(-1, M.shape[-2], M.shape[-1])
    orig_dtype = M.dtype
    M = M.bfloat16()
    coeffs = [
        (1, -alpha),
        (1, alpha),
    ]
    transpose = M.shape[-1] > M.shape[-2]
    if transpose:
        M = M.mT
    for a, b in coeffs:
        A = M @ M.mT
        M = a * M + b * A @ M
    if transpose:
        M = M.mT
    
    if len(orig_shape) > 2:
        M = M.reshape(orig_shape)
    return M.to(orig_dtype)

def soft_cap_coupling(w_max: float, wd: float, max_update_norm: float) -> float:
    """Calculates the strength for soft cap that bounds singular values at w_max."""
    k = w_max * (1 - wd) + max_update_norm
    coeffs = torch.tensor([-k**9, 3 * k**7, -3 * k**5, 0.0, k - w_max], dtype=torch.float32)
    monic_coeffs = coeffs / coeffs[0]
    n = monic_coeffs.numel() - 1
    comp = torch.zeros((n, n), dtype=torch.float32)
    comp[1:, :-1] = torch.eye(n - 1)
    comp[0, :] = -monic_coeffs[1:]
    roots = torch.linalg.eigvals(comp)
    is_real = torch.abs(roots.imag) < 1e-6
    is_nonnegative = roots.real >= 0
    padded_reals = torch.where(is_real & is_nonnegative, roots.real, torch.ones_like(roots.real))
    return float(torch.min(padded_reals))

@torch.compile
def _power_iteration(M: Tensor) -> float:
    """Power iteration to estimate the maximum singular value of a matrix."""
    # For a matrix M, shape is (out_features, in_features)
    u = torch.randn(M.size(0), device=M.device)  # out_features
    for _ in range(26):  # 32 power iteration steps
        v = u @ M  # u M
        u = v @ M.t()  # v M^T
        u = u / (u.norm() + 1e-8)
    sigma = torch.norm(u @ M)
    return sigma

class Muon(torch.optim.Optimizer):
    """
    Muon - MomentUm Orthogonalized by Newton-schulz

    https://kellerjordan.github.io/posts/muon/

    Muon internally runs standard SGD-momentum, and then performs an orthogonalization post-
    processing step, in which each 2D parameter's update is replaced with the nearest orthogonal
    matrix. To efficiently orthogonalize each update, we use a Newton-Schulz iteration, which has
    the advantage that it can be stably run in bfloat16 on the GPU.

    Some warnings:
    - This optimizer should not be used for the embedding layer, the final fully connected layer,
    or any {0,1}-D parameters; those should all be optimized by a standard method (e.g., AdamW).
    - To use it with 4D convolutional filters, it works well to just flatten their last 3 dimensions.

    Arguments:
        lr: The learning rate used by the internal SGD.
        momentum: The momentum used by the internal SGD.
        nesterov: Whether to use Nesterov-style momentum in the internal SGD. (recommended)
        ns_steps: The number of Newton-Schulz iteration steps to use.
    """
    def __init__(self, params, lr=0.02, momentum=0.95, nesterov=True, ns_steps=5, w_max=1, rank=0, world_size=1):
        self.rank = rank
        self.world_size = world_size
        defaults = dict(lr=lr, momentum=momentum, nesterov=nesterov, w_max=w_max, ns_steps=ns_steps)
        params: list[Tensor] = [*params]
        param_groups = []
        for size in {p.numel() for p in params}:
            b = torch.empty(world_size, size, dtype=torch.bfloat16, device="cuda")
            group = dict(params=[p for p in params if p.numel() == size],
                         update_buffer=b, update_buffer_views=[b[i] for i in range(world_size)])
            param_groups.append(group)
        super().__init__(param_groups, defaults)

    @torch.no_grad()
    def step(self):
        for group in self.param_groups:
            update_buffer: Tensor = group["update_buffer"]
            update_buffer_views: list[Tensor] = group["update_buffer_views"]
            # generate weight updates in distributed fashion
            params: list[Tensor] = group["params"]
            handle = None
            params_world = None
            def update_prev(): # optimized Muon implementation contributed by @YouJiacheng
                handle.wait()
                for p_world, g_world in zip(params_world, update_buffer_views):
                    scale = torch.sqrt(torch.tensor(p_world.size(-2) / p_world.size(-1)))
                    p_world.add_(g_world.view_as(p_world),
                                 alpha=-group["lr"] * scale)
                    
                    # Then apply soft cap projection with proper scaling
                    #max_update_norm = group["lr"]   # since orthogonalize(G) * scale has unit RMS->RMS norm
                    #alpha = soft_cap_coupling(group["w_max"], 0.0, max_update_norm * 1.14502 * 1.05)
                    #p_world.copy_(scale * soft_cap(p_world / scale, alpha))

                    sigma_max = _power_iteration(p_world).to(p_world.device)
                    scale_down = torch.maximum(torch.tensor(1.0, device=p_world.device), sigma_max / (group["w_max"]*scale))    # ABLATE THIS MAX
                    #if torch.isnan(scale_down):
                    #    scale_down = 1
                    p_world.div_(scale_down + 1e-12)
            for base_i in range(len(params))[::self.world_size]:
                if base_i + self.rank < len(params):
                    p = params[base_i + self.rank]
                    g = p.grad
                    assert g is not None
                    state = self.state[p]
                    if "momentum_buffer" not in state:
                        state["momentum_buffer"] = torch.zeros_like(g)
                    buf: Tensor = state["momentum_buffer"]
                    buf.lerp_(g, 1 - group["momentum"])
                    g = g.lerp_(buf, group["momentum"]) if group["nesterov"] else buf
                    g = orthogonalize(g).flatten() # zeropower_via_newtonschulz5(g, steps=group["ns_steps"]).flatten()
                else:
                    g = update_buffer_views[self.rank]
                if base_i > 0:
                    update_prev() # async all_gather instead of sync all_reduce by @YouJiacheng
                handle = dist.all_gather_into_tensor(update_buffer, g, async_op=True)
                params_world = params[base_i : base_i + self.world_size]
            update_prev()

# -----------------------------------------------------------------------------
# PyTorch nn.Module definitions for the model

class CastedLinear(nn.Linear):
    def __init__(self, in_features: int, out_features: int, W_max: float=1., use_fp8=False, x_s=1.0, w_s=1.0, grad_s=1.0):
        super().__init__(in_features, out_features, bias=False)
        self.W_max = W_max
        self.use_fp8 = use_fp8
        self.x_s = x_s
        self.w_s = w_s
        self.grad_s = grad_s

    def reset_parameters(self) -> None:
        std = 0.5 * (self.in_features ** -0.5) # 0.5 is a bit better than the default 1/sqrt(3)
        bound = (3 ** 0.5) * std
        with torch.no_grad():
            self.weight.uniform_(-bound, bound)

    def forward(self, x: Tensor):
        if self.use_fp8 and self.training:
            _x = x.flatten(0, -2)
            out: Tensor = torch.ops.nanogpt.mm(_x, self.weight, x_s=self.x_s, w_s=self.w_s, grad_s=self.grad_s)[0]
            return out.reshape(*x.shape[:-1], -1)
            # return out.reshape(*x.shape[:-1], -1) / self.W_max
        else:
            return F.linear(x, self.weight.type_as(x))
            # return F.linear(x, self.weight.type_as(x)) / self.W_max

class Rotary(nn.Module):
    def __init__(self, dim: int, max_seq_len: int):
        super().__init__()
        # half-truncate RoPE by @YouJiacheng (w/ base freq tuning)
        angular_freq = (1 / 1024) ** torch.linspace(0, 1, steps=dim//4, dtype=torch.float32)
        angular_freq = torch.cat([angular_freq, angular_freq.new_zeros(dim//4)])
        t = torch.arange(max_seq_len, dtype=torch.float32)
        theta = torch.einsum("i,j -> ij", t, angular_freq)
        self.cos = nn.Buffer(theta.cos(), persistent=False)
        self.sin = nn.Buffer(theta.sin(), persistent=False)

    def forward(self, x_BTHD: Tensor):
        assert self.cos.size(0) >= x_BTHD.size(-3)
        cos, sin = self.cos[None, :x_BTHD.size(-3), None, :], self.sin[None, :x_BTHD.size(-3), None, :]
        x1, x2 = x_BTHD.to(dtype=torch.float32).chunk(2, dim=-1)
        y1 = x1 * cos + x2 * sin
        y2 = x1 * (-sin) + x2 * cos
        return torch.cat((y1, y2), 3).type_as(x_BTHD)

class CausalSelfAttention(nn.Module):
    def __init__(self, dim: int, num_heads: int, max_seq_len: int, head_dim=128, W_max: float=1.):
        super().__init__()
        self.num_heads = num_heads
        self.head_dim = head_dim
        self.softmax_scale = (head_dim**0.5) / (W_max**2)
        # self.softmax_scale = head_dim**0.5
        hdim = num_heads * head_dim
        self.attn_q = CastedLinear(dim, hdim, W_max=W_max)
        self.attn_k = CastedLinear(dim, hdim, W_max=W_max)
        self.attn_v = CastedLinear(dim, hdim, W_max=W_max)
        self.rotary = Rotary(head_dim, max_seq_len)
        self.c_proj = CastedLinear(hdim, dim, W_max=W_max)
        self.c_proj.weight.detach().zero_() # zero init suggested by @Grad62304977

    def forward(self, x: Tensor, block_mask: BlockMask):
        B, T = x.size(0), x.size(1) # batch size, sequence length
        assert B == 1, "Must use batch size = 1 for FlexAttention"
        q = self.attn_q(x).view(B, T, self.num_heads, self.head_dim)
        k = self.attn_k(x).view(B, T, self.num_heads, self.head_dim)
        v = self.attn_v(x).view(B, T, self.num_heads, self.head_dim)
        q, k = self.rotary(q), self.rotary(k)
        # scale the attention logits by given constant, instead of the default head_dim**-0.5, by @leloykun
        # inspired by learnable scalars used by @brendanh0gan https://x.com/hi_tysam/status/1879693583898591283
        y = flex_attention(q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2), block_mask=block_mask, scale=self.softmax_scale/self.head_dim).transpose(1, 2)
        y = y.contiguous().view(B, T, self.num_heads * self.head_dim) # re-assemble all head outputs side by side
        y = self.c_proj(y / 3)
        return y

class MLP(nn.Module):
    def __init__(self, dim: int, W_max: float=1.):
        super().__init__()
        hdim = 4 * dim
        self.c_fc = CastedLinear(dim, hdim, W_max=W_max)
        self.c_proj = CastedLinear(hdim, dim, W_max=W_max)
        self.c_proj.weight.detach().zero_() # zero init suggested by @Grad62304977

    def forward(self, x: Tensor):
        x = self.c_fc(x)
        x = F.gelu(x) / 1.1289  # 1.1289 is the max derivative of gelu(x)
        x = self.c_proj(x)
        return x

class Block(nn.Module):
    def __init__(self, dim: int, num_heads: int, max_seq_len: int, num_layers: int, W_max: float=1.):
        super().__init__()
        self.attn = CausalSelfAttention(dim, num_heads, max_seq_len, W_max=W_max)
        self.mlp = MLP(dim, W_max=W_max)
        self.residual_scale = 1
        self.res_denom = 2*num_layers

    def forward(self, x: Tensor, block_mask: BlockMask):
        x = (1 - self.residual_scale/self.res_denom) * x + (self.residual_scale/self.res_denom) * self.attn(x, block_mask)
        x = (1 - self.residual_scale/self.res_denom) * x + (self.residual_scale/self.res_denom) * self.mlp(x)
        return x

# -----------------------------------------------------------------------------
# The main model

def next_multiple_of_n(v: float | int, *, n: int):
    return next(x for x in range(n, int(v) + 1 + n, n) if x >= v)

class GPT(nn.Module):
    def __init__(
        self, vocab_size: int, num_layers: int, num_heads: int, model_dim: int, max_seq_len: int,
        emb_w_max: float=1., W_max: float=1., lm_head_w_max: float=1.,
    ):
        super().__init__()
        self.embed = nn.Embedding(vocab_size, model_dim, max_norm=emb_w_max * model_dim**0.5)
        # token value embeddings by @KoszarskyB - inspired by @Grad62304977's value residual implementation following https://arxiv.org/abs/2410.17897
        # value embedding code simplification inspired by @ragulpr https://github.com/KellerJordan/modded-nanogpt/pull/78
        # self.value_embeds = nn.ModuleList([nn.Embedding(vocab_size, model_dim) for _ in range(3)])
        self.blocks = nn.ModuleList([Block(model_dim, num_heads, max_seq_len, num_layers, W_max) for i in range(num_layers)])
        # there are only 50257 unique GPT-2 tokens; we extend to nearest multiple of 128 for efficiency.
        # suggested to me by @Grad62304977. this originates from Karpathy's experiments.
        self.lm_head = CastedLinear(model_dim, next_multiple_of_n(vocab_size, n=128), W_max=lm_head_w_max)
                                    # use_fp8=True, x_s=(model_dim**0.5)/448, w_s=24/448, grad_s=1/448)
        self.lm_head.weight.detach().zero_() # @Grad62304977
        # Add learnable skip connection weights for decoder layers
        assert num_layers % 2 == 0

    def create_blockmasks(self, input_seq: Tensor, sliding_window_num_blocks: Tensor):
        BLOCK_SIZE = 128
        docs = (input_seq == 50256).cumsum(0)

        def document_causal(b, h, q_idx, kv_idx):
            causal_mask = q_idx >= kv_idx
            document_mask = docs[q_idx] == docs[kv_idx]
            return causal_mask & document_mask

        def dense_to_ordered(dense_blockmask: Tensor):
            num_blocks = dense_blockmask.sum(dim=-1, dtype=torch.int32)
            indices = dense_blockmask.argsort(dim=-1, descending=False, stable=True).flip(-1).to(torch.int32)
            return num_blocks[None, None].contiguous(), indices[None, None].contiguous()

        # manual block mask creation by @YouJiacheng
        assert len(input_seq) % BLOCK_SIZE == 0
        NUM_BLOCKS = len(input_seq) // BLOCK_SIZE
        block_idx = torch.arange(NUM_BLOCKS, dtype=torch.int32, device="cuda")
        causal_blockmask_any = block_idx[:, None] >= block_idx
        causal_blockmask_all = block_idx[:, None] > block_idx
        docs_low = docs.view(-1, BLOCK_SIZE)[:, 0].contiguous()
        docs_high = docs.view(-1, BLOCK_SIZE)[:, -1].contiguous()
        document_blockmask_any = (docs_low[:, None] <= docs_high) & (docs_high[:, None] >= docs_low)
        document_blockmask_all = (docs_low[:, None] == docs_high) & (docs_high[:, None] == docs_low)
        blockmask_any = causal_blockmask_any & document_blockmask_any
        blockmask_all = causal_blockmask_all & document_blockmask_all
        partial_kv_num_blocks, partial_kv_indices = dense_to_ordered(blockmask_any & ~blockmask_all)
        full_kv_num_blocks, full_kv_indices = dense_to_ordered(blockmask_all)
        def build_bm(window_size_blocks: Tensor) -> BlockMask:
            return BlockMask.from_kv_blocks(
                torch.clamp_max(partial_kv_num_blocks, torch.clamp_min(window_size_blocks - full_kv_num_blocks, 1)),
                partial_kv_indices,
                torch.clamp_max(full_kv_num_blocks, window_size_blocks - 1),
                full_kv_indices,
                BLOCK_SIZE=BLOCK_SIZE,
                mask_mod=document_causal,
            )
        # Long-short SWA block masks by @leloykun & @YouJiacheng, adapated from suggestion by @Grad62304977, following Gemma 2 paper
        return build_bm(sliding_window_num_blocks), build_bm(sliding_window_num_blocks // 2)

    def forward(self, input_seq: Tensor, target_seq: Tensor, sliding_window_num_blocks: Tensor, return_logits_argmax: bool = False):
        assert input_seq.ndim == 1
        long_bm, short_bm = self.create_blockmasks(input_seq, sliding_window_num_blocks)
        block_masks = [long_bm, short_bm, short_bm, short_bm, long_bm, short_bm, short_bm, long_bm, short_bm, short_bm, short_bm, long_bm]
        assert len(block_masks) == len(self.blocks)

        x = self.embed(input_seq)[None]

        max_act_rms_norm_list = [None, None, None, None, None, None, None, None, None, None, None, None, None, None]
        max_act_entry_list    = [None, None, None, None, None, None, None, None, None, None, None, None, None, None]
        max_act_rms_norm_list[0] = x.norm(dim=-1).max() / (x.size(-1)**0.5)
        max_act_entry_list[0] = x.max()

        for i in range(len(self.blocks)):
            x = self.blocks[i](x, block_masks[i])
            max_act_rms_norm_list[i + 1] = x.norm(dim=-1).max() / (x.size(-1)**0.5)
            max_act_entry_list[i + 1] = x.max()

        logits = self.lm_head(x).float()
        max_act_rms_norm_list[-1] = logits.norm(dim=-1).max() / (logits.size(-1)**0.5)
        max_act_entry_list[-1] = logits.max()
        # @Grad62304977 added tanh softcapping following Gemma 2 paper, @KoszarskyB reduced it from 30 to 15, @YouJiacheng shifted it by +15 (2*sigmoid(2*x)=tanh(x)+1)
        # logits = 30 * torch.sigmoid(logits / (7.5 * x.size(-1)**0.5))
        loss = F.cross_entropy(logits.view(-1, logits.size(-1)), target_seq, reduction='sum' if self.training else 'mean')
        max_logits = logits.max(dim=-1)[1] if return_logits_argmax else None
        return loss, max_logits, max_act_rms_norm_list, max_act_entry_list

# -----------------------------------------------------------------------------
# Our own simple Distributed Data Loader

def _load_data_shard(file: Path):
    header = torch.from_file(str(file), False, 256, dtype=torch.int32) # header is 256 int32
    assert header[0] == 20240520, "magic number mismatch in the data .bin file"
    assert header[1] == 1, "unsupported version"
    num_tokens = int(header[2]) # number of tokens (claimed)
    with file.open("rb", buffering=0) as f:
        tokens = torch.empty(num_tokens, dtype=torch.uint16, pin_memory=True) # avoid pin_memory copy by @YouJiacheng
        f.seek(256 * 4)
        nbytes = f.readinto(tokens.numpy()) # avoid bytes->array copy by @YouJiacheng
        assert nbytes == 2 * num_tokens, "number of tokens read does not match header"
    return tokens

def distributed_data_generator(filename_pattern: str, batch_size: int, rank : int, world_size : int):
    files = [Path(file) for file in sorted(glob.glob(filename_pattern))]
    assert batch_size % world_size == 0
    local_batch_size = batch_size // world_size
    file_iter = iter(files) # use itertools.cycle(files) instead if you want to do multi-epoch training
    tokens, pos = _load_data_shard(next(file_iter)), 0
    while True:
        if pos + batch_size + 1 >= len(tokens):
            tokens, pos = _load_data_shard(next(file_iter)), 0
        buf = tokens[pos + rank * local_batch_size:][:local_batch_size + 1]
        inputs = buf[:-1].to(device="cuda", dtype=torch.int32, non_blocking=True) # no sync on host side;
        targets = buf[1:].to(device="cuda", dtype=torch.int64, non_blocking=True) # H2D in another stream isn't helpful.
        pos += batch_size
        yield inputs, targets

# -----------------------------------------------------------------------------
# int main

@dataclass
class Hyperparameters:
    # data
    train_files = "data/fineweb10B/fineweb_train_*.bin" # input .bin to train on
    val_files = "data/fineweb10B/fineweb_val_*.bin" # input .bin to eval validation loss on
    val_tokens = 10485760 # how many tokens of validation data? it's important to keep this fixed for consistent comparisons
    train_seq_len = 48*1024 # FlexAttention sequence length
    val_seq_len = 4*64*1024 # FlexAttention sequence length for validation
    # arch configs
    emb_w_max = 1
    w_max = 8
    lm_head_w_max = 8  # equivalent to inverse temperature
    lm_head_muon = True
    # optimization
    num_iterations = 1770 # number of iterations to run
    cooldown_frac = 0.4 # fraction of training spent cooling down the learning rate
    # architecture
    vocab_size = 50257
    # evaluation and logging
    val_loss_every = 125 # every how many steps to evaluate val loss? 0 for only at the end
    save_checkpoint = False
args = Hyperparameters()

import argparse
parser = argparse.ArgumentParser(description="Train a GPT model")
parser.add_argument("--head_lr", type=float, default=0.005, help="learning rate for head")
parser.add_argument("--qkv_lr", type=float, default=0.05, help="learning rate for QKV weights")
parser.add_argument("--hidden_lr", type=float, default=0.05, help="learning rate for hidden layers")
parser_args = parser.parse_args()
print(f"head_lr: {parser_args.head_lr}")
print(f"qkv_lr: {parser_args.qkv_lr}")
print(f"hidden_lr: {parser_args.hidden_lr}")

# torchrun sets these env variables
rank = int(os.environ["RANK"])
world_size = int(os.environ["WORLD_SIZE"])
assert world_size == 8 # this code is designed for 8xH100
assert torch.cuda.is_available()
device = torch.device("cuda", int(os.environ["LOCAL_RANK"]))
torch.cuda.set_device(device)
dist.init_process_group(backend="nccl", device_id=device)
dist.barrier()
master_process = (rank == 0) # this process will do logging, checkpointing etc.

# begin logging
logfile = None
if master_process:
    run_id = uuid.uuid4()
    os.makedirs("logs", exist_ok=True)
    logfile = f"logs/{run_id}.txt"
    print(logfile)
def print0(s, console=False):
    if master_process:
        with open(logfile, "a") as f:
            if console:
                print(s)
            print(s, file=f)

# begin by printing this file (the Python code)
print0(code)
print0("="*100)
# log information about the hardware/software environment this is running on
print0(f"Running Python {sys.version}")
print0(f"Running PyTorch {torch.version.__version__} compiled for CUDA {torch.version.cuda}")
def nvidia_smi():
    import subprocess  # avoid top level import
    return subprocess.run(["nvidia-smi"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout
print0(nvidia_smi())
print0("="*100)

########################################
#    Construct model and optimizer     #
########################################

model: nn.Module = GPT(vocab_size=args.vocab_size, num_layers=12, num_heads=6, model_dim=768,
                       emb_w_max=args.emb_w_max, W_max=args.w_max, lm_head_w_max=args.lm_head_w_max,
                       max_seq_len=max(args.train_seq_len, args.val_seq_len)).cuda()
for m in model.modules():
    if isinstance(m, nn.Embedding):
        m.bfloat16()
for param in model.parameters():
    dist.broadcast(param.detach(), 0)

# collect the parameters to optimize
hidden_matrix_params = [p for n, p in model.blocks.named_parameters() if p.ndim >= 2 and "embed" not in n and "attn_" not in n]
qkv_params = [p for n, p in model.blocks.named_parameters() if p.ndim >= 2 and "attn_" in n]
embed_params = [p for n, p in model.named_parameters() if "embed" in n]
scalar_params = [p for p in model.parameters() if p.ndim < 2]
head_params = [model.lm_head.weight]

# current training has no scalar parameters
assert len(scalar_params) == 0

@torch.compile
def project_embed_unembed_weights(model):
    with torch.no_grad():
        if not args.lm_head_muon:
            # Normalize head matrix rows [shape vocab_size, d_embed]
            lm_head_w_max_tensor = torch.tensor(args.lm_head_w_max, dtype=torch.float32, device=device)
            rms = torch.norm(model.lm_head.weight, dim=-1, keepdim=True) * model.lm_head.weight.shape[-1]**0.5
            model.lm_head.weight.div_(torch.maximum(rms, lm_head_w_max_tensor) / lm_head_w_max_tensor + 1e-12)
project_embed_unembed_weights(model)
# Approximately orthogonalize hidden matrix parameters at init
with torch.no_grad():
    for p in hidden_matrix_params + qkv_params:
        p.copy_(orthogonalize(p).float() * torch.sqrt(torch.tensor(p.size(0) / p.size(1))))

# init the optimizer(s)
# 
if args.lm_head_muon:
    adam_params = [dict(params=embed_params, lr=0.1)]
else:
    adam_params = [dict(params=head_params, lr=parser_args.head_lr), dict(params=embed_params, lr=0.1)]
# small adam epsilon by @YouJiacheng. this is an alternate method of fixing the world_size dependence
# discovered by @fernbear.bsky.social https://x.com/hi_tysam/status/1879692937589875094
optimizer1 = torch.optim.Adam(adam_params, betas=(0.8, 0.95), eps=1e-10, fused=True)
optimizer2 = Muon(hidden_matrix_params, lr=parser_args.hidden_lr, momentum=0.95, w_max=args.w_max, rank=rank, world_size=world_size)
optimizer3 = Muon(qkv_params, lr=parser_args.qkv_lr, momentum=0.95, w_max=args.w_max, rank=rank, world_size=world_size)
if args.lm_head_muon:
    optimizer4 = Muon(head_params, lr=parser_args.head_lr, momentum=0.95, w_max=args.lm_head_w_max, rank=rank, world_size=world_size)
    optimizers = [optimizer1, optimizer2, optimizer3, optimizer4]  # optimizers[1:] must all be muon optimizers for momentum warmup
else:
    optimizers = [optimizer1, optimizer2, optimizer3]
for opt in optimizers:
    for group in opt.param_groups:
        group["initial_lr"] = group["lr"]

# learning rate schedule: stable then decay
def get_lr(step: int):
    x = step / args.num_iterations # progress in training
    assert 0 <= x < 1
    if x < 1 - args.cooldown_frac:
        return 1.0
    else:
        w = (1 - x) / args.cooldown_frac
        return w * 1.0 + (1 - w) * 0.1

# attention window size schedule: linearly increase
@lru_cache(1)
def get_window_size_blocks_helper(window_size: int):
    return torch.tensor(window_size // 128, dtype=torch.int32, pin_memory=True).cuda(non_blocking=True)
def get_window_size_blocks(step: int):
    x = step / args.num_iterations # progress in training
    assert 0 <= x <= 1
    # Linearly increase the block-wise sliding window size over training 128 -> 1792
    # increase by @fernbear.bsky.social; block-wise by @YouJiacheng
    window_size = next_multiple_of_n(1728 * x, n=128)
    return get_window_size_blocks_helper(window_size)

model: nn.Module = torch.compile(model, dynamic=False)

########################################
#            Warmup kernels            #
########################################

# Warmup the training kernels, then re-initialize the state so we aren't cheating
warmup_steps = 10
initial_state = dict(model=copy.deepcopy(model.state_dict()),
                     optimizers=[copy.deepcopy(opt.state_dict()) for opt in optimizers]) # save the initial state
for _ in range(warmup_steps):
    inputs = targets = torch.randint(0, args.vocab_size, size=(args.train_seq_len,), device="cuda")
    loss, _, _, _ = model(inputs.to(torch.int32), targets, get_window_size_blocks(0))
    loss.backward()
    for param in model.parameters():
        dist.all_reduce(param.grad, op=dist.ReduceOp.AVG)
    for opt in optimizers:
        opt.step()
    model.zero_grad(set_to_none=True)
model.load_state_dict(initial_state["model"])
for opt, opt_state in zip(optimizers, initial_state["optimizers"]):
    opt.load_state_dict(opt_state)
del initial_state

########################################
#        Training and validation       #
########################################

train_loader = distributed_data_generator(args.train_files, world_size * args.train_seq_len, rank, world_size)
training_time_ms = 0
# start the clock
torch.cuda.synchronize()
t0 = time.perf_counter()
# begin training
train_steps = args.num_iterations
for step in range(train_steps + 1):
    last_step = (step == train_steps)

    # --------------- VALIDATION SECTION -----------------
    if last_step or (args.val_loss_every > 0 and step % args.val_loss_every == 0):
        # stop the clock
        torch.cuda.synchronize()
        training_time_ms += 1000 * (time.perf_counter() - t0)
        model.eval()

        # print all weight norms
        rms_to_rms_norm = lambda w: torch.linalg.norm(w.float(), ord=2)*(w.shape[1]/w.shape[0])**0.5
        if master_process:
            print(">>> Weights:")
            for name, weight in model.named_parameters():
                weight_shape = str(tuple(weight.shape))
                if "embed" in name:
                    print0(f"{name:<40} {weight_shape:<13}:  l1->RMS:{torch.max(weight.norm(dim=-1)) / weight.shape[-1]**0.5:.4f}, RMS->RMS:{rms_to_rms_norm(weight):.4f}", console=True)
                elif "lm_head" in name:
                    print0(f"{name:<40} {weight_shape:<13}: RMS->INF:{torch.max(weight.norm(dim=-1)) * weight.shape[-1]**0.5:.4f}, RMS->RMS:{rms_to_rms_norm(weight):.4f}", console=True)
                elif len(weight.shape) == 3:
                    for i, w in enumerate(weight):
                        print0(f"{name:<37} #{i:<2} {weight_shape:<13}: RMS->RMS:{rms_to_rms_norm(w):.4f}", console=True)
                else:
                    print0(f"{name:<40} {weight_shape:<13}: RMS->RMS:{rms_to_rms_norm(weight):.4f}", console=True)

        # run val step
        val_batch_size = world_size * args.val_seq_len
        assert args.val_tokens % val_batch_size == 0
        val_steps = args.val_tokens // val_batch_size
        val_loader = distributed_data_generator(args.val_files, val_batch_size, rank, world_size)
        val_loss = 0
        val_correct = 0
        val_total = 0
        with torch.no_grad():
            for val_step in range(val_steps):
                inputs, targets = next(val_loader)
                loss, pred, max_act_rms_norm_list, max_act_entry_list = model(inputs, targets, get_window_size_blocks(step), return_logits_argmax=True)
                if master_process and val_step == 0:
                    print(">>> Act RMS Norms:")
                    print0(f"Embed:     {max_act_rms_norm_list[0]:.4f}", console=True)
                    for i, max_act_rms_norm in enumerate(max_act_rms_norm_list[1:-1]):
                        print0(f"Block #{i}: {max_act_rms_norm:.4f}", console=True)
                    print0(f"Logits:    {max_act_rms_norm_list[-1]:.4f}", console=True)
                    print(">>> Act Max Entries:")
                    print0(f"Embed:     {max_act_entry_list[0]:.4f}", console=True)
                    for i, max_entry in enumerate(max_act_entry_list[1:-1]):
                        print0(f"Block #{i}: {max_entry:.4f}", console=True)
                    print0(f"Logits:    {max_act_entry_list[-1]:.4f}", console=True)
                val_loss += loss
                val_correct += (pred == targets).sum().item()
                val_total += targets.numel()
        val_loss /= val_steps
        val_acc = val_correct / val_total
        del val_loader
        dist.all_reduce(val_loss, op=dist.ReduceOp.AVG)
        dist.all_reduce(torch.tensor([val_correct, val_total], device="cuda"), op=dist.ReduceOp.SUM)
        val_acc = val_correct / val_total
        print0(f"step:{step}/{train_steps} val_loss:{val_loss:.4f} val_acc:{val_acc:.4f} train_time:{training_time_ms:.0f}ms step_avg:{training_time_ms/max(step, 1):.2f}ms", console=True)
        model.train()
        # start the clock again
        torch.cuda.synchronize()
        t0 = time.perf_counter()

    if last_step:
        if master_process and args.save_checkpoint:
            log = dict(step=step, code=code, model=model.state_dict(), optimizers=[opt.state_dict() for opt in optimizers])
            os.makedirs(f"logs/{run_id}", exist_ok=True)
            torch.save(log, f"logs/{run_id}/state_step{step:06d}.pt")
        # the last step only has the validation loop, so break to avoid training
        break

    # --------------- TRAINING SECTION -----------------
    inputs, targets = next(train_loader)
    loss, _, _, _ = model(inputs, targets, get_window_size_blocks(step))
    loss.backward()
    for param in model.parameters():
        dist.all_reduce(param.grad, op=dist.ReduceOp.AVG)
    # Print grads
    if last_step or (args.val_loss_every > 0 and step % args.val_loss_every == 0):
        # stop the clock
        torch.cuda.synchronize()
        training_time_ms += 1000 * (time.perf_counter() - t0)
        if master_process:
            print(">>> Grads:")
            for name, weight in model.named_parameters():
                if weight.grad is None:
                    continue
                weight_shape = str(tuple(weight.shape))
                if "embed" in name:
                    print0(f"{name:<40} {weight_shape:<13}:  l1->RMS:{torch.max(weight.grad.norm(dim=-1)) / weight.shape[-1]**0.5:.4f}, RMS->RMS:{rms_to_rms_norm(weight.grad):.4f}", console=True)
                elif "lm_head" in name:
                    print0(f"{name:<40} {weight_shape:<13}: RMS->INF:{torch.max(weight.grad.norm(dim=-1)) * weight.shape[-1]**0.5:.4f}, RMS->RMS:{rms_to_rms_norm(weight.grad):.4f}", console=True)
                else:
                    print0(f"{name:<40} {weight_shape:<13}: RMS->RMS:{rms_to_rms_norm(weight.grad):.4f}", console=True)
        # start the clock again
        torch.cuda.synchronize()
        t0 = time.perf_counter()
    # set optimization hyperparameters
    for opt in optimizers:
        for group in opt.param_groups:
            group["lr"] = group["initial_lr"] * get_lr(step)
    for opt_muon in optimizers[1:]:
        for group in opt_muon.param_groups:
            frac = min(step / 300, 1) # momentum warmup for muon
            group["momentum"] = (1 - frac) * 0.85 + frac * 0.95
    for opt in optimizers:
        opt.step()
    project_embed_unembed_weights(model)
    # null the gradients
    model.zero_grad(set_to_none=True)
    # logging
    approx_training_time_ms = training_time_ms + 1000 * (time.perf_counter() - t0)
    if (step + 1) % 10 == 0:
        print0(f"step:{step+1}/{train_steps} train_time:{approx_training_time_ms:.0f}ms step_avg:{approx_training_time_ms/(step + 1):.2f}ms", console=True)

print0(f"peak memory allocated: {torch.cuda.max_memory_allocated() // 1024 // 1024} MiB "
       f"reserved: {torch.cuda.max_memory_reserved() // 1024 // 1024} MiB", console=True)
dist.destroy_process_group()