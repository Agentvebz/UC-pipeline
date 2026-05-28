"""
slide_layout.py — Dynamic layout engine for PPTX slides.

Distributes content blocks evenly across the available vertical space.
When there's less content, everything stretches. When there's more, it compresses.
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Block:
    """A content block with minimum and preferred heights."""
    name: str
    min_h: float        # Minimum height in inches
    preferred_h: float  # Preferred height if space allows
    stretch: float = 1.0  # How much this block wants to grow (weight)
    actual_h: float = 0.0  # Calculated height after layout
    y: float = 0.0         # Calculated y position after layout


@dataclass
class Gap:
    """A gap between blocks."""
    min_h: float = 0.08
    preferred_h: float = 0.15
    stretch: float = 0.5
    actual_h: float = 0.0
    y: float = 0.0


def compute_layout(blocks: list, top: float, bottom: float) -> list:
    """
    Distribute blocks + gaps evenly across [top, bottom] vertical space.
    
    Each block gets at least min_h. Remaining space is distributed
    proportionally by stretch weight.
    
    Args:
        blocks: List of Block and Gap objects in order
        top: Top of usable area (inches)
        bottom: Bottom of usable area (inches)
    
    Returns:
        Same list with y and actual_h computed for each item
    """
    available = bottom - top
    
    # Step 1: Give everyone their minimum
    total_min = sum(b.min_h for b in blocks)
    
    if total_min >= available:
        # Not enough space — compress to minimums
        scale = available / total_min if total_min > 0 else 1.0
        y = top
        for b in blocks:
            b.y = y
            b.actual_h = b.min_h * scale
            y += b.actual_h
        return blocks
    
    # Step 2: Give everyone their preferred height
    total_preferred = sum(b.preferred_h for b in blocks)
    
    if total_preferred <= available:
        # More space than preferred — distribute excess by stretch weight
        excess = available - total_preferred
        total_stretch = sum(b.stretch for b in blocks)
        
        y = top
        for b in blocks:
            bonus = (b.stretch / total_stretch * excess) if total_stretch > 0 else 0
            b.actual_h = b.preferred_h + bonus
            b.y = y
            y += b.actual_h
    else:
        # Between min and preferred — interpolate
        # How far between min and preferred can we go?
        range_total = total_preferred - total_min
        surplus = available - total_min
        ratio = surplus / range_total if range_total > 0 else 1.0
        
        y = top
        for b in blocks:
            extra = (b.preferred_h - b.min_h) * ratio
            b.actual_h = b.min_h + extra
            b.y = y
            y += b.actual_h
    
    return blocks