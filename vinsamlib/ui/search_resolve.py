"""
Turns a bare index.db.SearchResult (disconnected from any live Volume or
parsed bank) back into a real ui.models.TreeNode the existing Detail/Samples
panes can render unchanged — by re-opening the container and walking the
stored chain (folder/bank names, preset native ids) back down to the hit,
exactly as the scanner walked it when building the index.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .models import TreeNode
from ..banks import e4b, krz
from ..index.db import SearchResult
from ..vfs.detect import open_volume, sniff


def resolve_result(result: SearchResult) -> Optional[TreeNode]:
    container_path = result.container_path
    if sniff(container_path) is None:
        return _resolve_loose_bank(container_path, result)
    return _resolve_in_image(container_path, result)


def _resolve_loose_bank(container_path: str, result: SearchResult) -> Optional[TreeNode]:
    try:
        data = Path(container_path).read_bytes()
    except OSError:
        return None
    fmt, bank = _parse_bank_bytes(data, container_path)
    if bank is None:
        return None
    bank_node = TreeNode("bank", Path(container_path).name, None, None,
                          handle=bank, format_label=fmt)
    if result.kind == "bank":
        return bank_node
    preset_native_id = result.chain[-1].native_id if result.chain else None
    preset_obj = _find_preset(bank, fmt, preset_native_id)
    if preset_obj is None:
        return bank_node
    return TreeNode("preset", result.name, bank_node, (bank, preset_obj))


def _resolve_in_image(container_path: str, result: SearchResult) -> Optional[TreeNode]:
    vol = open_volume(container_path)
    if vol is None:
        return None
    current_entry = None
    node: Optional[TreeNode] = None
    for chain_entry in result.chain:
        if chain_entry.kind in ("folder", "bank"):
            found = next((e for e in vol.list(current_entry) if e.name == chain_entry.native_id), None)
            if found is None:
                return node
            current_entry = found
            node = TreeNode(chain_entry.kind, found.name, node, (vol, found), size=found.size)
            if chain_entry.kind == "bank":
                try:
                    data = vol.read(found)
                except Exception:
                    continue
                fmt, bank = _parse_bank_bytes(data, found.name)
                if bank is not None:
                    node.handle = bank
                    node.format_label = fmt
        elif chain_entry.kind == "preset" and node is not None and node.handle is not None:
            preset_obj = _find_preset(node.handle, node.format_label, chain_entry.native_id)
            if preset_obj is None:
                return node
            node = TreeNode("preset", chain_entry.name, node, (node.handle, preset_obj))
    return node


def _find_preset(bank, fmt: str, native_id: Optional[str]):
    if native_id is None:
        return None
    try:
        if fmt == "E4B":
            idx = int(native_id)
            return next((p for p in bank.presets if p.index == idx), None)
        if fmt == "KRZ":
            return bank.programs.get(int(native_id))
    except (ValueError, AttributeError):
        return None
    return None


def _parse_bank_bytes(data: bytes, label: str):
    if data[:4] == b"FORM" and data[8:12] == b"E4B0":
        try:
            return "E4B", e4b.parse_bytes(data, label)
        except Exception:
            return "E4B", None
    if data[:4] == b"PRAM":
        try:
            return "KRZ", krz.parse_bytes(data, label)
        except Exception:
            return "KRZ", None
    return "", None
