"""Load the required BARO functions from a checksum-pinned RCAEval checkout."""

import ast
import hashlib

import numpy as np
import pandas as pd
from sklearn.preprocessing import RobustScaler


PIN = "dc9f48786db50cf9469f2c596b79f01907d4ac91"
SOURCES = {
    "RCAEval/io/time_series.py": (
        "37cc5223f99d241f091484851d0d9933ef6a42f4e124e0f2e0a85356eab2b1cd",
        {"preprocess", "drop_time", "drop_constant", "convert_mem_mb", "drop_extra", "drop_near_constant", "select_useful_cols"},
    ),
    "RCAEval/e2e/baro.py": (
        "3cf4e9c4f0b3d1a07ec23cd82169e869b17bcf01fb195c0ae5edca10baeda165",
        {"baro"},
    ),
}


def upstream_core(vendor):
    namespace = {"np": np, "pd": pd, "RobustScaler": RobustScaler}
    for relative_path, (expected_hash, functions) in SOURCES.items():
        path = vendor / relative_path
        source = path.read_bytes()
        if hashlib.sha256(source).hexdigest() != expected_hash:
            raise ValueError(f"RCAEval source checksum mismatch: {relative_path}")
        nodes = [node for node in ast.parse(source, filename=str(path)).body
                 if isinstance(node, ast.FunctionDef) and node.name in functions]
        if {node.name for node in nodes} != functions:
            raise ValueError(f"RCAEval source functions are incomplete: {relative_path}")
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["baro"]
