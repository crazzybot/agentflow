"""
=============================================================================
LSTM USER GUIDE — Part 1: Minimal LSTM Implementation (PyTorch)
=============================================================================

What is an LSTM?
----------------
A Long Short-Term Memory (LSTM) network is a special kind of Recurrent Neural
Network (RNN) designed to learn patterns in *sequences* — data where the order
matters (e.g., text, time series, audio).

Standard RNNs struggle to remember events from far back in a sequence (the
"vanishing gradient" problem). LSTMs solve this with a *cell state* — a kind
of long-term memory — controlled by three learned gates:

  • Forget gate  — decides what to throw away from memory
  • Input gate   — decides what new information to store
  • Output gate  — decides what part of memory to expose as the next hidden state

Architecture at a glance
-------------------------
        ┌────────────────────────────────────────┐
        │              LSTM Cell                 │
  x_t ──►  forget ──► input ──► cell ──► output ──► h_t
        │                          ▲               │
        │                     (cell state c_t)     │
        └────────────────────────────────────────┘

This file shows:
  1. How to build a minimal LSTM model class in PyTorch.
  2. How to run a forward pass with dummy data.
  3. How to print and inspect the model's parameters.
=============================================================================
"""

import torch                        # Core PyTorch library
import torch.nn as nn               # Neural network building blocks


# ---------------------------------------------------------------------------
# 1.  Define the LSTM Model
# ---------------------------------------------------------------------------

class MinimalLSTM(nn.Module):
    """
    A minimal, single-layer LSTM that maps an input sequence to a single
    output value (regression) or to a class score (classification).

    Parameters
    ----------
    input_size  : int  — number of features per time-step (e.g., 1 for univariate)
    hidden_size : int  — number of units in the LSTM's hidden state
    output_size : int  — number of output values (1 for regression, N for N classes)
    num_layers  : int  — how many LSTM layers to stack (default = 1)
    dropout     : float— dropout probability between stacked layers (default = 0.0)
    """

    def __init__(
        self,
        input_size: int,
        hidden_size: int,
        output_size: int,
        num_layers: int = 1,
        dropout: float = 0.0,
    ) -> None:
        super().__init__()                          # Always call parent __init__

        self.hidden_size = hidden_size
        self.num_layers  = num_layers

        # ── LSTM layer ──────────────────────────────────────────────────────
        # batch_first=True means input shape is (batch, seq_len, features)
        # which is easier to work with than PyTorch's default (seq_len, batch, features)
        self.lstm = nn.LSTM(
            input_size  = input_size,
            hidden_size = hidden_size,
            num_layers  = num_layers,
            batch_first = True,
            dropout     = dropout if num_layers > 1 else 0.0,  # dropout needs >1 layer
        )

        # ── Fully-connected output layer ────────────────────────────────────
        # Takes the LAST hidden state and maps it to the desired output size
        self.fc = nn.Linear(hidden_size, output_size)

    # -----------------------------------------------------------------------
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass through the LSTM.

        Parameters
        ----------
        x : torch.Tensor, shape (batch_size, seq_len, input_size)
            The input sequence batch.

        Returns
        -------
        out : torch.Tensor, shape (batch_size, output_size)
            Predictions for the last time-step of each sequence.
        """
        batch_size = x.size(0)

        # ── Initialise hidden and cell states to zeros ──────────────────────
        # Shape: (num_layers, batch_size, hidden_size)
        h0 = torch.zeros(self.num_layers, batch_size, self.hidden_size)
        c0 = torch.zeros(self.num_layers, batch_size, self.hidden_size)

        # ── Run the LSTM ─────────────────────────────────────────────────────
        # lstm_out : all hidden states,  shape (batch, seq_len, hidden_size)
        # (hn, cn) : final hidden & cell state — not used here
        lstm_out, (_hn, _cn) = self.lstm(x, (h0, c0))

        # ── Take only the LAST time-step's hidden state ─────────────────────
        # lstm_out[:, -1, :] → shape (batch, hidden_size)
        last_hidden = lstm_out[:, -1, :]

        # ── Map hidden state → output ────────────────────────────────────────
        out = self.fc(last_hidden)          # shape (batch, output_size)
        return out


# ---------------------------------------------------------------------------
# 2.  Instantiate and Inspect the Model
# ---------------------------------------------------------------------------

def main() -> None:
    """Demonstrate the minimal LSTM with a dummy forward pass."""

    # ── Hyper-parameters ────────────────────────────────────────────────────
    INPUT_SIZE  = 1     # one feature per time-step
    HIDDEN_SIZE = 32    # LSTM memory width
    OUTPUT_SIZE = 1     # single regression output
    SEQ_LEN     = 10    # sequence length (look-back window)
    BATCH_SIZE  = 4     # number of sequences per batch

    # ── Build model ─────────────────────────────────────────────────────────
    model = MinimalLSTM(
        input_size  = INPUT_SIZE,
        hidden_size = HIDDEN_SIZE,
        output_size = OUTPUT_SIZE,
    )
    print("=" * 60)
    print("Model Architecture")
    print("=" * 60)
    print(model)

    # ── Count trainable parameters ───────────────────────────────────────────
    total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"\nTotal trainable parameters: {total_params:,}")

    # ── Create random dummy input ────────────────────────────────────────────
    # Shape: (batch_size, seq_len, input_size)
    dummy_input = torch.randn(BATCH_SIZE, SEQ_LEN, INPUT_SIZE)
    print(f"\nInput shape : {tuple(dummy_input.shape)}  "
          f"(batch={BATCH_SIZE}, seq_len={SEQ_LEN}, features={INPUT_SIZE})")

    # ── Forward pass ────────────────────────────────────────────────────────
    model.eval()                        # disable dropout/batchnorm during inference
    with torch.no_grad():               # no gradient tracking needed here
        output = model(dummy_input)

    print(f"Output shape: {tuple(output.shape)}  "
          f"(batch={BATCH_SIZE}, output_size={OUTPUT_SIZE})")
    print(f"Sample output values: {output.squeeze().tolist()}")
    print("\n✓ Forward pass successful!")

    # ── LSTM gate weight shapes ──────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Named Parameters (name → shape)")
    print("=" * 60)
    for name, param in model.named_parameters():
        print(f"  {name:<40} {tuple(param.shape)}")


if __name__ == "__main__":
    main()
