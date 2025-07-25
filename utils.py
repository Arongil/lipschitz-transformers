import time
import json
import jax
import psutil
import numpy as np
from pathlib import Path


def calculate_lipschitz_constant(output: dict) -> float:
    """Calculate Lipschitz constant for a model."""
    if output['parameters']['data'] == "cifar":
        mlp_in_norm = output['results']['mlp_in']['weight_norm'][-1]
        mlp_0_norm = output['results']['mlp_0']['weight_norm'][-1]
        mlp_out_norm = output['results']['mlp_out']['weight_norm'][-1]
        return mlp_in_norm * mlp_0_norm * mlp_out_norm
    
    # assume the model is a transformer
    L_embed = output['results']['embed']['weight_norm'][-1]
    L_unembed = output['results']['out']['weight_norm'][-1]
    attention_scale = output['parameters']['softmax_scale']
    
    num_layers = output['parameters']['num_blocks']
    alpha = 1 / (2*num_layers)
    
    # Stage 1: Calculate maximum activation norms
    # Start with embedding norm = 1
    act_norms = [1.0]  # x_0 has norm 1
    
    # Track norms through each layer
    for layer in range(num_layers):
        # Get attention weights for this layer
        q_norm = output['results'][f'q{layer}']['weight_norm'][-1]
        k_norm = output['results'][f'k{layer}']['weight_norm'][-1]
        v_norm = output['results'][f'v{layer}']['weight_norm'][-1]
        w_norm = output['results'][f'w{layer}']['weight_norm'][-1]

        # Attention block increases norm by W_out * V norm
        att_norm = 1/3 * w_norm * v_norm
        act_norms.append((1 - alpha) * act_norms[-1] + alpha * att_norm * act_norms[-1])
        
        # Get MLP weights for this layer
        mlp_in_norm = output['results'][f'mlp_in{layer}']['weight_norm'][-1]
        mlp_out_norm = output['results'][f'mlp_out{layer}']['weight_norm'][-1]
        
        # MLP block increases norm by W_out * W_in norm / 1.1289
        mlp_norm = mlp_out_norm * mlp_in_norm / 1.1289  # Divide by GeLU max derivative
        act_norms.append((1 - alpha) * act_norms[-1] + alpha * mlp_norm * act_norms[-1])

    # Stage 2: calculate lipschitz constant
    L = L_embed
    for layer in range(num_layers):
        q_norm = output['results'][f'q{layer}']['weight_norm'][-1]
        k_norm = output['results'][f'k{layer}']['weight_norm'][-1]
        v_norm = output['results'][f'v{layer}']['weight_norm'][-1]
        w_norm = output['results'][f'w{layer}']['weight_norm'][-1]

        # Attention block Lipschitz constant
        # W_out norm * (V norm + V act norm * (Q norm * K act norm + Q act norm * K norm))
        L_att = 1/3 * w_norm * (v_norm + 2 * v_norm * act_norms[2*layer]**2 * q_norm * k_norm) * attention_scale
        
        # Get MLP weights for this layer
        mlp_in_norm = output['results'][f'mlp_in{layer}']['weight_norm'][-1]
        mlp_out_norm = output['results'][f'mlp_out{layer}']['weight_norm'][-1]

        # MLP block Lipschitz constant
        # L_mlp = W_out * W_in / 1.1289
        L_mlp = mlp_out_norm * mlp_in_norm / 1.1289
        
        # Update total Lipschitz constant through residual connections
        L = (1 - alpha) * L + alpha * L * L_att
        L = (1 - alpha) * L + alpha * L * L_mlp
    
    # Multiply by unembedding Lipschitz constant
    L = L * L_unembed
    return L


class Logger:
    def __init__(self, config):
        self.config = config
        self.start_time = time.time()
        self.log = {}
        self.intermediate_results = {
            "losses": [],
            "val_losses": [],
            "accuracies": [],
            "train_accuracies": [],
        }

    def log_training(self, step, loss, accuracy, log):
        """Log training metrics."""
        # Calculate ETA
        steps_done = (1 + step) * (1 + self.config.val_iters / self.config.val_interval)
        steps_remaining = (self.config.steps - step) * (
            1 + self.config.val_iters / self.config.val_interval
        )
        eta_seconds = (time.time() - self.start_time) * steps_remaining / steps_done
        eta_str = time.strftime("%H:%M:%S", time.gmtime(eta_seconds))

        # Log metrics
        self.intermediate_results["losses"].append(float(loss))
        self.intermediate_results["train_accuracies"].append(float(accuracy))
        self.log = log

        # Print log message
        memory_stats = jax.device_get(jax.devices()[0].memory_stats())
        gpu_memory = memory_stats["peak_bytes_in_use"] / 1024**3 if memory_stats else -1
        process = psutil.Process()
        ram_usage = process.memory_info().rss / 1024**3

        print(
            f"[{time.strftime('%H:%M:%S')} gpu {gpu_memory:.1f}G ram {ram_usage:.1f}G] "
            + f"Step:{step}/{self.config.steps} train_loss:{loss:.4f} train_acc:{accuracy:.4f} ETA:{eta_str}"
        )

    def log_validation(self, step, metrics):
        """Log validation metrics."""
        self.intermediate_results["val_losses"].append(float(metrics["loss"]))
        self.intermediate_results["accuracies"].append(float(metrics["accuracy"]))

        print(
            f"  Step:{step}/{self.config.steps} val_loss:{metrics['loss']:.4f} val_acc:{metrics['accuracy']:.4f}"
        )

    def get_results(self):
        """Return all results."""
        self.log["total_time"] = time.time() - self.start_time
        self.log = {**self.log, **self.intermediate_results}
        return self.log


def save_results(results, weights_checkpoints, model, config):
    """Save results and model checkpoints."""
    Path(config.output_dir).mkdir(parents=True, exist_ok=True)

    # Generate unique filename
    timestamp = time.strftime("%Y%m%d_%H%M%S") + f"{int(time.time() * 1000) % 1000:03d}"
    config.timestamp = timestamp
    uniqueness_hash = hash(json.dumps(config))

    filename = (
        f"MLP_{config.project['default']}_{config.data}_{config.optimizer}_"
        f"embed{config.d_embed}_lr{config.lr:.4f}_"
        f"wd{config.wd:.4f}_steps{config.steps}_"
        f"{abs(uniqueness_hash):x}.json"
    )

    # Create output dictionary
    output = {
        "parameters": config,
        "results": results,
    }

    # Calculate Lipschitz constant
    lipschitz_constant = calculate_lipschitz_constant(output)
    output["results"]["lipschitz_constant"] = lipschitz_constant

    # Convert JAX arrays to Python lists
    output = jax_to_numpy(output)

    # Save results
    output_path = Path(config.output_dir) / filename
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    # Save checkpoints if enabled
    if config.num_checkpoints > 0:
        model_dict = {
            "args": config,
            "results": results,
        }
        for i, checkpoint in enumerate(weights_checkpoints):
            model_dict[f"weights_checkpoint_{i}"] = {
                i: w for i, w in enumerate(checkpoint)
            }

        model_dict = jax_to_numpy(model_dict)
        model_path = (
            Path(config.output_dir) / f"{config.data}_{config.optimizer}_"
            + f"val_loss_{results['val_losses'][-1]:.3f}_"
            + f"acc_{results['accuracies'][-1]:.3f}_"
            + f"lipschitz_{lipschitz_constant:.3f}.npz"
        )

        np.savez_compressed(model_path, **model_dict, allow_pickle=True)
        print(f"Checkpoints saved to {model_path}")


def jax_to_numpy(d):
    """Recursively convert JAX arrays to Python lists."""
    import jax.numpy as jnp

    if isinstance(d, dict):
        return {k: jax_to_numpy(v) for k, v in d.items()}
    elif isinstance(d, list):
        return [jax_to_numpy(v) for v in d]
    elif isinstance(d, jnp.ndarray):
        return d.tolist()
    else:
        return d
