"""Ableton Live 12 control surface for the M-VAVE SMC-PAD."""


def create_instance(c_instance):
    from .surface import HybridElectro

    return HybridElectro(c_instance)
