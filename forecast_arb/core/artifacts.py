"""
Centralized artifact writing for deterministic I/O.

All artifact writes should use these helpers to ensure:
- Directory creation
- Consistent formatting
- Error handling
- Audit trails
"""

import json
import yaml
from pathlib import Path
from typing import Any, Dict, Union


def ensure_dir(path: Union[str, Path]) -> Path:
    """
    Ensure directory exists, creating if necessary.
    
    Args:
        path: Directory path (str or Path)
        
    Returns:
        Path object for the directory
    """
    dir_path = Path(path)
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path


def write_json(path: Union[str, Path], obj: Any, indent: int = 2) -> None:
    """
    Write object to JSON file with consistent formatting.
    
    Args:
        path: Output file path
        obj: Object to serialize
        indent: JSON indentation (default: 2)
    """
    file_path = Path(path)
    ensure_dir(file_path.parent)
    
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=indent, ensure_ascii=False)


def write_yaml(path: Union[str, Path], obj: Any) -> None:
    """
    Write object to YAML file.
    
    Args:
        path: Output file path
        obj: Object to serialize
    """
    file_path = Path(path)
    ensure_dir(file_path.parent)
    
    with open(file_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(obj, f, default_flow_style=False, sort_keys=False)


def write_text(path: Union[str, Path], text: str) -> None:
    """
    Write text to file.
    
    Args:
        path: Output file path
        text: Text content
    """
    file_path = Path(path)
    ensure_dir(file_path.parent)
    
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(text)


def read_json(path: Union[str, Path]) -> Any:
    """
    Read JSON file.
    
    Args:
        path: Input file path
        
    Returns:
        Deserialized object
    """
    file_path = Path(path)
    
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def read_yaml(path: Union[str, Path]) -> Any:
    """
    Read YAML file.
    
    Args:
        path: Input file path
        
    Returns:
        Deserialized object
    """
    file_path = Path(path)
    
    with open(file_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)
