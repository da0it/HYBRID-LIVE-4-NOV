"""Second Live control surface; uses the single shared HybridElectro config."""


def create_instance(c_instance):
    from HybridElectro.pads import HybridElectroPads

    return HybridElectroPads(c_instance)
