"""
=============================================================================
LSTM USER GUIDE — Part 2: Time-Series Prediction
=============================================================================

Goal
----
Predict the *next value* in a synthetic sine-wave time series using an LSTM.

Concepts covered
----------------
  • Sliding-window dataset creation
  • Min-Max normalisation (scaling data to [0, 1])
  • Train / validation / test split
  • Training loop with loss tracking
  • Inverse-transforming predictions back to original scale
  • Evaluation with MAE and RMSE

Dataset
-------
We generate a noisy sine wave:  y(t) = sin(t) + noise
The LSTM looks at the last SEQ_LEN values and predicts the next one.
=============================================================================
"""

import math
import random

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset


# ---------------------------------------------------------------------------
# 0.  Reproducibility — fix all random seeds
# ---------------------------------------------------------------------------

SEED = 42
random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)


# ---------------------------------------------------------------------------
# 1.  Data Generation
# ---------------------------------------------------------------------------

def generate_sine_wave(
    n_points: int = 1000,
    noise_std: float = 0.05,
) -> np.ndarray:
    """
    Generate a noisy sine wave with `n_points` samples.

    Parameters
    ----------
    n_points  : total number of data points
    noise_std : standard deviation of Gaussian noise added to the signal

    Returns
    -------
    signal : np.ndarray, shape (n_points,)
    """
    t      = np.linspace(0, 4 * math.pi, n_points)   # evenly spaced time steps
    signal = np.sin(t) + np.random.normal(0, noise_std, n_points)
    return signal.astype(np.float32)


# ---------------------------------------------------------------------------
# 2.  Min-Max Scaler (hand-rolled so there are no extra dependencies)
# ---------------------------------------------------------------------------

class MinMaxScaler:
    """
    Scales data to the range [0, 1].

    Attributes
    ----------
    min_ : float  — minimum value seen during fit
    max_ : float  — maximum value seen during fit
    """

    def __init__(self) -> None:
        self.min_: float = 0.0
        self.max_: float = 1.0

    def fit(self, data: np.ndarray) -> "MinMaxScaler":
        """Learn the min and max from `data`."""
        self.min_ = float(data.min())
        self.max_ = float(data.max())
        return self

    def transform(self, data: np.ndarray) -> np.ndarray:
        """Scale data to [0, 1]."""
        return (data - self.min_) / (self.max_ - self.min_ + 1e-8)

    def inverse_transform(self, data: np.ndarray) -> np.ndarray:
        """Reverse the scaling back to the original range."""
        return data * (self.max_ - self.min_ + 1e-8) + self.min_


# ---------------------------------------------------------------------------
# 3.  Sliding-Window Dataset
# ---------------------------------------------------------------------------

class TimeSeriesDataset(Dataset):
    """
    PyTorch Dataset that creates (input_window, target) pairs using a
    sliding window over a 1-D time series.

    Example with seq_len=3:
      series = [1, 2, 3, 4, 5]
      sample 0: X=[1,2,3]  y=4
      sample 1: X=[2,3,4]  y=5

    Parameters
    ----------
    series  : 1-D numpy array of float32
    seq_len : length of the look-back window
    """

    def __init__(self, series: np.ndarray, seq_len: int) -> None:
        self.series  = series
        self.seq_len = seq_len

    def __len__(self) -> int:
        # number of valid windows that fit inside the series
        return len(self.series) - self.seq_len

    def __getitem__(self, idx: int):
        # X: window of seq_len values → shape (seq_len, 1)  [1 feature]
        x = self.series[idx : idx + self.seq_len].reshape(-1, 1)
        # y: the single value immediately after the window
        y = self.series[idx + self.seq_len].reshape(1)
        return torch.tensor(x), torch.tensor(y)


# ---------------------------------------------------------------------------
# 4.  LSTM Model (same structure as Part 1, repeated here for clarity)
# ---------------------------------------------------------------------------

class LSTMForecaster(nn.Module):
    """
    Single-layer LSTM that maps a time-series window → next value.

    Parameters
    ----------
    input_size  : features per time-step (1 for univariate)
    hidden_size : LSTM hidden units
    output_size : prediction size (1 for one-step-ahead)
    num_layers  : stacked LSTM layers
    """

    def __init__(
        self,
        input_size: int = 1,
        hidden_size: int = 64,
        output_size: int = 1,
        num_layers: int = 2,
    ) -> None:
        super().__init__()
        self.hidden_size = hidden_size
        self.num_layers  = num_layers

        self.lstm = nn.LSTM(
            input_size  = input_size,
            hidden_size = hidden_size,
            num_layers  = num_layers,
            batch_first = True,
            dropout     = 0.2,   # regularisation between LSTM layers
        )
        self.fc = nn.Linear(hidden_size, output_size)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        b = x.size(0)
        h0 = torch.zeros(self.num_layers, b, self.hidden_size)
        c0 = torch.zeros(self.num_layers, b, self.hidden_size)
        out, _ = self.lstm(x, (h0, c0))
        return self.fc(out[:, -1, :])


# ---------------------------------------------------------------------------
# 5.  Training Loop
# ---------------------------------------------------------------------------

def train_one_epoch(
    model: nn.Module,
    loader: DataLoader,
    optimizer: torch.optim.Optimizer,
    criterion: nn.Module,
) -> float:
    """
    Run one full training epoch.

    Returns
    -------
    avg_loss : float — mean loss over all batches
    """
    model.train()
    total_loss = 0.0

    for x_batch, y_batch in loader:
        optimizer.zero_grad()           # clear previous gradients
        predictions = model(x_batch)   # forward pass
        loss = criterion(predictions, y_batch)
        loss.backward()                 # compute gradients
        # Gradient clipping prevents exploding gradients (common in RNNs)
        nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()               # update weights
        total_loss += loss.item()

    return total_loss / len(loader)


@torch.no_grad()
def evaluate(
    model: nn.Module,
    loader: DataLoader,
    criterion: nn.Module,
) -> float:
    """Run evaluation (no gradient computation)."""
    model.eval()
    total_loss = 0.0
    for x_batch, y_batch in loader:
        predictions = model(x_batch)
        total_loss += criterion(predictions, y_batch).item()
    return total_loss / len(loader)


# ---------------------------------------------------------------------------
# 6.  Main Script
# ---------------------------------------------------------------------------

def main() -> None:
    # ── Hyper-parameters ────────────────────────────────────────────────────
    SEQ_LEN    = 20     # look-back window length
    BATCH_SIZE = 32
    EPOCHS     = 30
    LR         = 1e-3   # learning rate
    HIDDEN     = 64
    N_LAYERS   = 2

    # ── 6.1  Generate & scale data ───────────────────────────────────────────
    print("Generating sine-wave data...")
    raw_series = generate_sine_wave(n_points=1000)

    scaler = MinMaxScaler()
    scaler.fit(raw_series)
    scaled = scaler.transform(raw_series)       # values now in [0, 1]

    # ── 6.2  Train / val / test split  (70 / 15 / 15) ────────────────────────
    n       = len(scaled)
    n_train = int(n * 0.70)
    n_val   = int(n * 0.15)

    train_data = scaled[:n_train]
    val_data   = scaled[n_train : n_train + n_val]
    test_data  = scaled[n_train + n_val :]

    print(f"Split sizes — train: {len(train_data)}, "
          f"val: {len(val_data)}, test: {len(test_data)}")

    # ── 6.3  Build datasets & loaders ────────────────────────────────────────
    train_ds = TimeSeriesDataset(train_data, SEQ_LEN)
    val_ds   = TimeSeriesDataset(val_data,   SEQ_LEN)
    test_ds  = TimeSeriesDataset(test_data,  SEQ_LEN)

    train_loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True)
    val_loader   = DataLoader(val_ds,   batch_size=BATCH_SIZE)
    test_loader  = DataLoader(test_ds,  batch_size=BATCH_SIZE)

    # ── 6.4  Model, loss, optimiser ──────────────────────────────────────────
    model     = LSTMForecaster(hidden_size=HIDDEN, num_layers=N_LAYERS)
    criterion = nn.MSELoss()                        # Mean Squared Error for regression
    optimizer = torch.optim.Adam(model.parameters(), lr=LR)

    # Learning rate scheduler: halve LR if val loss stalls for 5 epochs
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="min", factor=0.5, patience=5
    )

    # ── 6.5  Training loop ───────────────────────────────────────────────────
    print("\nTraining...")
    print(f"{'Epoch':>6}  {'Train Loss':>12}  {'Val Loss':>10}")
    print("-" * 34)

    best_val_loss = float("inf")
    best_state    = None

    for epoch in range(1, EPOCHS + 1):
        train_loss = train_one_epoch(model, train_loader, optimizer, criterion)
        val_loss   = evaluate(model, val_loader, criterion)
        scheduler.step(val_loss)

        # Save best model weights
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state    = {k: v.clone() for k, v in model.state_dict().items()}

        if epoch % 5 == 0 or epoch == 1:
            print(f"{epoch:>6}  {train_loss:>12.6f}  {val_loss:>10.6f}")

    # ── 6.6  Restore best weights & evaluate on test set ────────────────────
    model.load_state_dict(best_state)
    test_loss = evaluate(model, test_loader, criterion)
    print(f"\nTest MSE  (scaled)  : {test_loss:.6f}")

    # ── 6.7  Compute MAE and RMSE on the test set (original scale) ────────────
    model.eval()
    preds_list, actuals_list = [], []

    with torch.no_grad():
        for x_batch, y_batch in test_loader:
            preds_list.append(model(x_batch).numpy())
            actuals_list.append(y_batch.numpy())

    preds   = np.concatenate(preds_list).flatten()
    actuals = np.concatenate(actuals_list).flatten()

    # Inverse-transform both back to original scale
    preds_orig   = scaler.inverse_transform(preds)
    actuals_orig = scaler.inverse_transform(actuals)

    mae  = np.mean(np.abs(preds_orig - actuals_orig))
    rmse = math.sqrt(np.mean((preds_orig - actuals_orig) ** 2))

    print(f"Test MAE  (original): {mae:.4f}")
    print(f"Test RMSE (original): {rmse:.4f}")
    print("\n✓ Time-series prediction complete!")


if __name__ == "__main__":
    main()
