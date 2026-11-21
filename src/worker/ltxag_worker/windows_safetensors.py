"""Read large PyTorch checkpoints through a read-only Windows mapping."""

import json
import math
import mmap
import os
import struct


class ReadOnlyCheckpoint:
    def __init__(self, filename, device):
        import torch

        self.torch = torch
        self.device = device
        with open(filename, "rb") as source:
            self.mapping = mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            header_size = struct.unpack_from("<Q", self.mapping)[0]
            self.data_start = 8 + header_size
            if not 0 < header_size <= 100_000_000 or self.data_start > len(self.mapping):
                raise ValueError("Invalid safetensors header size")
            self.index = json.loads(self.mapping[8:self.data_start])
            self.info = self.index.pop("__metadata__", None)
            self.dtypes = {
                "BF16": torch.bfloat16, "F16": torch.float16, "F32": torch.float32,
                "F64": torch.float64, "BOOL": torch.bool, "U8": torch.uint8,
                "I8": torch.int8, "I16": torch.int16, "I32": torch.int32, "I64": torch.int64,
                "F8_E4M3": torch.float8_e4m3fn, "F8_E5M2": torch.float8_e5m2,
            }
        except BaseException:
            self.mapping.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.mapping.close()

    def keys(self):
        return sorted(self.index)

    def metadata(self):
        return self.info

    def get_tensor(self, key):
        entry = self.index[key]
        dtype = self.dtypes[entry["dtype"]]
        shape = entry["shape"]
        start, end = entry["data_offsets"]
        size = end - start
        if (not 0 <= start <= end <= len(self.mapping) - self.data_start
                or any(dim < 0 for dim in shape)
                or size != math.prod(shape) * dtype.itemsize):
            raise ValueError(f"Invalid safetensors data range for {key}")
        if size == 0:
            return self.torch.empty(shape, dtype=dtype, device=self.device)
        # The tensor must own writable storage after the read-only file view closes.
        data = bytearray(size)
        with memoryview(data) as target, memoryview(self.mapping) as mapped:
            with mapped[self.data_start + start:self.data_start + end] as source:
                target[:] = source
        return self.torch.frombuffer(data, dtype=dtype).reshape(shape).to(device=self.device)


def install():
    import safetensors
    import safetensors.torch

    original = safetensors.safe_open

    def safe_open(filename, framework, device="cpu", **kwargs):
        if framework in ("pt", "pytorch") and os.path.getsize(filename) >= 8 * 2**30:
            # The native reader initially uses a copy-on-write mapping even
            # with backend="pread", which can reserve the full file in commit.
            return ReadOnlyCheckpoint(filename, device)
        return original(filename, framework, device, **kwargs)

    safetensors.safe_open = safe_open
    safetensors.torch.safe_open = safe_open
