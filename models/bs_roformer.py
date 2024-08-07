import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import Module, ModuleList
import torchaudio
from einops import rearrange
import numpy as np
from rotary_embedding_torch import RotaryEmbedding

from models.fourier import Fourier
from models.attend import Attend


FREQ_BINS_PER_BANDS = [
  2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
  2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
  2, 2, 2, 2,
  4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
  12, 12, 12, 12, 12, 12, 12, 12,
  24, 24, 24, 24, 24, 24, 24, 24,
  48, 48, 48, 48, 48, 48, 48, 48,
  128, 129,
]


class ModelArgs:
    n_embd: int = 512
    n_layers: int = 6
    n_heads: int = 16


class ModelArgs:
    n_fft: int = 2048
    hop_length: int = 441
    n_embd: int = 512
    depth: int = 6
    n_heads: int = 16


class BSRoformer(Fourier):
    def __init__(
        self,
        n_fft: int = 2048,
        hop_length: int = 441,
        depth: int = 6,
        dim: int = 512,
        n_heads: int = 16
    ):
        super().__init__(n_fft, hop_length)

        self.depth = depth
        self.dim = dim
        self.n_heads = n_heads

        self.cmplx_num = 2
        self.audio_channels = 2
        self.time_bins = 4
        
        self.head_dim = self.dim // self.n_heads

        band_input_dims = self.cmplx_num * self.audio_channels * self.time_bins * np.array(FREQ_BINS_PER_BANDS)
        band_input_dims = list(band_input_dims)

        self.band_split = BandSplit(
            band_input_dims=band_input_dims,
            dim=dim,
        )

        self.band_combine = BandCombine(
            dim=dim,
            band_output_dims=band_input_dims
        )

        time_rotary_embed = RotaryEmbedding(dim=self.head_dim)
        freq_rotary_embed = RotaryEmbedding(dim=self.head_dim)

        self.transformers = ModuleList([])

        for _ in range(self.depth):
            self.transformers.append(nn.ModuleList([
                TransformerBlock(dim=self.dim, n_heads=self.n_heads, rotary_embed=time_rotary_embed),
                TransformerBlock(dim=self.dim, n_heads=self.n_heads, rotary_embed=freq_rotary_embed)
            ]))
        
    def forward(self, mixture):
        """Separation model.

        Args:
            mixture: (batch_size, channels_num, samples_num)

        Outputs:
            output: (batch_size, channels_num, samples_num)
        """

        # Complex spectrum.
        complex_sp = self.stft(mixture)
        # shape: (B, C, T, F)

        batch_size = complex_sp.shape[0]
        time_steps = complex_sp.shape[2]

        x = self.process_image(complex_sp)

        x = torch.view_as_real(x)
        # shape: (B, C, T, F, Z)

        x = rearrange(x, 'b c (t m) f z -> b t (m f c z)', m=self.time_bins)

        x = self.band_split(x)
        # shape: (b, k, t, d)

        for t_transformer, f_transformer in self.transformers:

            x = rearrange(x, 'b k t d -> (b k) t d')

            x = t_transformer(x)

            x = rearrange(x, '(b k) t d -> (b t) k d', b=batch_size)

            x = f_transformer(x)

            x = rearrange(x, '(b t) k d -> b t k d', b=batch_size)

        x = self.band_combine(x)

        x = rearrange(x, 'b t (m f c z) -> b c (m t) f z', m=self.time_bins, c=self.audio_channels, z=self.cmplx_num)
        
        x = torch.view_as_complex(x) 

        mask = self.unprocess_image(x, time_steps)

        sep_stft = mask * complex_sp

        output = self.istft(sep_stft)

        return output

    def process_image(self, x):

        B, C, T, Freq = x.shape

        pad_len = (
            int(np.ceil(T / self.time_bins)) * self.time_bins
            - T
        )
        x = F.pad(x, pad=(0, 0, 0, pad_len))

        return x

    def unprocess_image(self, x, time_steps):
        """Patch a spectrum to the original shape. E.g.,
        
        Args:
            x: E.g., (B, C, 208, 1024)
        
        Outpus:
            output: E.g., (B, C, 201, 1025)
        """

        output = x[:, :, 0 : time_steps, :]

        return output


class RMSNorm(torch.nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6):
        r"""https://github.com/meta-llama/llama/blob/main/llama/model.py"""
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        norm_x = torch.mean(x ** 2, dim=-1, keepdim=True)
        output = x * torch.rsqrt(norm_x + self.eps) * self.weight
        return output


class BandSplit(Module):
    
    def __init__(
        self,
        band_input_dims: list[int],
        dim: int,
    ):
        super().__init__()
        
        self.band_input_dims = band_input_dims
        self.band_nets = ModuleList([])

        for in_dim in band_input_dims:

            net = nn.Sequential(
            
                # No Norm for the first layer
                nn.Linear(in_dim, dim),
                nn.GELU(),

                RMSNorm(dim),
                nn.Linear(dim, dim),
                nn.GELU(),

                RMSNorm(dim),
                nn.Linear(dim, dim)
            )

            self.band_nets.append(net)

    def forward(self, x):

        band_xs = torch.split(x, split_size_or_sections=self.band_input_dims, dim=-1)

        outputs = []
        for x, net in zip(band_xs, self.band_nets):
            output = net(x)
            outputs.append(output)

        return torch.stack(outputs, dim=1)


class BandCombine(Module):
    
    def __init__(
        self,
        dim: int,
        band_output_dims: list[int],
    ):
        super().__init__()
        
        self.band_output_dims = band_output_dims
        self.band_nets = ModuleList([])

        for out_dim in band_output_dims:

            net = nn.Sequential(

                RMSNorm(dim),
                nn.Linear(dim, dim),
                nn.GELU(),

                RMSNorm(dim),
                nn.Linear(dim, dim),
                nn.GELU(),

                RMSNorm(dim),
                nn.Linear(dim, out_dim)
            )

            self.band_nets.append(net)

    def forward(self, x):

        band_xs = torch.unbind(x, dim=1)

        outputs = []
        for x, net in zip(band_xs, self.band_nets):
            output = net(x)
            outputs.append(output)

        return torch.cat(outputs, dim=-1)


class MLP(nn.Module):
    def __init__(self, dim: int) -> None:
        super().__init__()

        self.fc1 = nn.Linear(dim, 4 * dim, bias=False)
        self.gelu = nn.GELU()
        self.fc2 = nn.Linear(4 * dim, dim, bias=False)

    def forward(self, x):
        x = self.fc1(x)
        x = self.gelu(x)
        x = self.fc2(x)
        return x


class Attention(nn.Module):

    def __init__(self, dim: int, n_heads: int, rotary_embed: RotaryEmbedding):
        super().__init__()
        
        assert dim % n_heads == 0

        self.n_heads = n_heads
        self.dim = dim
        self.rotary_embed = rotary_embed

        self.flash = hasattr(torch.nn.functional, 'scaled_dot_product_attention')
        assert self.flash, "Must have flash attention."
        
        self.c_attn = nn.Linear(dim, 3 * dim, bias=False)
        self.c_proj = nn.Linear(dim, dim, bias=False)
        
    def forward(self, x):
        r"""
        Args:
            x: (b, t, h*d)

        Constants:
            b: batch_size
            t: time steps
            r: 3
            h: heads_num
            d: heads_dim
        """
        B, T, C = x.size()

        q, k, v = rearrange(self.c_attn(x), 'b t (r h d) -> r b h t d', r=3, h=self.n_heads)
        # q, k, v: (b, h, t, d)

        q = self.rotary_embed.rotate_queries_or_keys(q)
        k = self.rotary_embed.rotate_queries_or_keys(k)

        if self.flash:
            y = torch.nn.functional.scaled_dot_product_attention(q, k, v, attn_mask=None, dropout_p=0, is_causal=False)
        
        y = rearrange(y, 'b h t d -> b t (h d)')

        y = self.c_proj(y)
        # shape: (b, t, h*d)

        return y


class TransformerBlock(nn.Module):
    def __init__(self, dim: int, n_heads: int, rotary_embed: RotaryEmbedding):
        
        super().__init__()
        self.dim = dim
        self.n_heads = n_heads
        
        self.att_norm = RMSNorm(dim)
        self.ffn_norm = RMSNorm(dim)
        self.att = Attention(dim=dim, n_heads=n_heads, rotary_embed=rotary_embed)
        self.mlp = MLP(dim=dim)
        

    def forward(
        self,
        x: torch.Tensor,
    ):
        h = x + self.att(self.att_norm(x))
        out = h + self.mlp(self.ffn_norm(h))
        return out
