import math
from typing import Dict, Any

import lightning as pl
import torch
import torch.nn as nn
import torch.nn.functional as F
from lightning.pytorch.utilities.types import STEP_OUTPUT


class TransformerModule(pl.LightningModule):
    def __init__(self, src_vocab_size, tgt_vocab_size, d_model=512, nhead=8, num_encoder_layers=6, num_decoder_layers=6,
                 dim_feedforward=2048, dropout=0.1, src_pad_idx=0, tgt_pad_idx=0):
        super().__init__()
        self.save_hyperparameters()

        # Encoder와 Decoder에 사용될 Embedding Layer 정의
        self.src_embedding = nn.Embedding(src_vocab_size, d_model)
        self.tgt_embedding = nn.Embedding(tgt_vocab_size, d_model)

        # Positional Encoding 추가
        self.positional_encoding = PositionalEncoding(d_model, dropout)

        # Transformer Encoder Layer
        encoder_layers = nn.TransformerEncoderLayer(d_model, nhead, dim_feedforward, dropout, batch_first=True)
        self.transformer_encoder = nn.TransformerEncoder(encoder_layers, num_encoder_layers)

        # Transformer Decoder Layer
        decoder_layers = nn.TransformerDecoderLayer(d_model, nhead, dim_feedforward, dropout, batch_first=True)
        self.transformer_decoder = nn.TransformerDecoder(decoder_layers, num_decoder_layers)

        # 최종 출력을 위한 Linear Layer
        self.out = nn.Linear(d_model, tgt_vocab_size)

        # Params
        self.src_pad_idx = src_pad_idx
        self.tgt_pad_idx = tgt_pad_idx

    def forward(self, src: torch.Tensor, tgt: torch.Tensor,
                src_mask=None, tgt_mask=None, src_padding_mask=None, tgt_padding_mask=None):
        memory = self.encode(src, src_mask=src_mask, src_padding_mask=src_padding_mask)
        tgt_embedded = self.positional_encoding(self.tgt_embedding(tgt))

        output = self.transformer_decoder(tgt=tgt_embedded, memory=memory, tgt_mask=tgt_mask,
                                          tgt_key_padding_mask=tgt_padding_mask)

        output = self.out(output)
        return output

    def encode(self, src: torch.Tensor, src_mask=None, src_padding_mask=None) -> torch.Tensor:
        """
        Encode output could be used later, depending on the project
        """
        src_embedded = self.positional_encoding(self.src_embedding(src))
        memory = self.transformer_encoder(src=src_embedded, mask=src_mask,
                                          src_key_padding_mask=src_padding_mask)
        return memory

    @staticmethod
    def _make_padding_mask(seq: torch.Tensor, pad_idx: int) -> torch.Tensor:
        return torch.tensor(seq == pad_idx).to(seq.device)

    def training_step(self, batch: Dict[str, torch.Tensor], batch_idx: int):
        """
        Original Transformer should be (max_sequence, batch, features).
        But when nn.TransformerEncoderLayer(..., batch_first=True), we can use it like (batch, max_sequence, features)

        batch
            - src: (batch, max_sequence)
            - tgt_input: (batch, max_sequence) but <eos> is missing at the end of sequence.
            - tgt_output: (batch, max_sequence) but <bos> is missing at the beginning of sequence.
        """
        # batch에서 소스 데이터, 타겟 데이터 분리
        src, tgt_input, tgt_output = batch['src'], batch['tgt_input'], batch['tgt_output']
        tgt_output = tgt_output.to(torch.long)

        # Create Padding Mask
        # [batch, max_sequence] -> (64, 128)
        # dtype: bool
        #  - masked: True
        #  - unmasked: False
        src_padding_mask = self._make_padding_mask(src, self.src_pad_idx)
        tgt_padding_mask = self._make_padding_mask(tgt_input, self.tgt_pad_idx)

        # Look Ahead Mask (A.K.A No Peak Mask)
        # [max_sequence, max_sequence] -> (128, 128)
        # dtype: float32
        #  - masked: -inf
        #  - unmasked: 0
        tgt_mask = nn.Transformer.generate_square_subsequent_mask(src.shape[1], device=src.device)

        output = self(src, tgt_input,
                      src_padding_mask=src_padding_mask,
                      tgt_padding_mask=tgt_padding_mask,
                      tgt_mask=tgt_mask)

        # Cross Entropy Loss
        # output shape [batch, max_sequence, vocab_size] (64, 128, 8000)
        #           -> [batch * max_sequence, vocab_size] (8192, 8000)
        # tgt_output shape [batch, max_sequence] (64, 128)
        #           -> [batch * max_sequence] (8192)
        loss = F.cross_entropy(output.view(-1, output.size(-1)), tgt_output.view(-1), ignore_index=self.tgt_pad_idx)

        # 로깅 (옵션)
        self.log('train_loss', loss, on_step=True, on_epoch=True, prog_bar=True, logger=True)
        self.log('loss', loss.item(), on_step=True, on_epoch=True, prog_bar=True, logger=True)
        self.log('mode', 0)
        return loss

    def validation_step(self, batch, batch_idx) -> STEP_OUTPUT:
        src, tgt_input, tgt_output = batch['src'], batch['tgt_input'], batch['tgt_output']
        tgt_output = tgt_output.to(torch.long)

        # Create Padding Mask
        # [batch, max_sequence] -> (64, 128)
        src_padding_mask = self._make_padding_mask(src, self.src_pad_idx)
        tgt_padding_mask = self._make_padding_mask(tgt_input, self.tgt_pad_idx)

        output = self(src, tgt_input,
                      src_padding_mask=src_padding_mask,
                      tgt_padding_mask=tgt_padding_mask)

        loss = F.cross_entropy(output.view(-1, output.size(-1)), tgt_output.view(-1), ignore_index=self.tgt_pad_idx)
        self.log('val_loss', loss, on_step=True, on_epoch=True, prog_bar=True, logger=True)
        self.log('loss', loss.item(), on_step=True, on_epoch=True, prog_bar=True, logger=True)
        self.log('mode', 1)

        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=0.0005)


class PositionalEncoding(nn.Module):
    def __init__(self, d_model, dropout=0.1, max_len=5000):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)

        # 위치 인코딩 테이블 초기화
        position = torch.arange(max_len).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2) * -(math.log(10000.0) / d_model))
        pe = torch.zeros(max_len, d_model)
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)

        pe = pe.unsqueeze(0).transpose(0, 1)
        # pe를 모듈의 버퍼로 등록하여 학습 중에 값이 업데이트되지 않도록 함
        self.register_buffer('pe', pe)

    def forward(self, x):
        """
        Args:
            x: Tensor, shape [seq_len, batch_size, embedding_dim]
        """
        # 입력 텐서에 위치 인코딩 값을 추가
        x = x + self.pe[:x.size(0), :]
        return self.dropout(x)