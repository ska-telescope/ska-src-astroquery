from .core import SRCNet
import pyvo
import os
import requests

### Base class
class SKAProduct:
    def __init__(self, namespace, name):
        self.namespace = namespace
        self.name = name

    ### Methods
    def show_methods(self):
        print("Available Format Factory methods:")
        for method in self.list_methods():
            print(f"- {method}")

    def subcube(self, circle=None, range_=None, polygon=None, output_file=None, **kwargs):
        SRCNet.soda_cutout(
            namespace=self.namespace,
            name=self.name,
            circle=circle,
            polygon=polygon,
            range_=range_,
            output_file=output_file,
            **kwargs
        )
        return output_file

### Data type: Image
class Image(SKAProduct):
    def list_methods(self):
        return [
            "subcube", 
            "display (placeholder)",
            "header_info (placeholder)",
            "fits_to_hdf5 (placeholder)"
        ]

### Data type: Cube
class Cube(SKAProduct):
    def list_methods(self):
        return [
            "subcube",
            "spectral_cut (placeholder)",
            "display (placeholder)",
            "header_info (placeholder)",
            "moment_map (placeholder)",
            "fits_to_hdf5 (placeholder)"
        ]

class SKAFormatFactory:
    def get_format_type(namespace, name):
        metadata = SRCNet.get_metadata(namespace, name)
        print("DEBUG: Metadata received:", metadata)
        return metadata.get("dataproduct_type", "unknown").lower()

    def get(namespace, name):
        format_type = SKAFormatFactory.get_format_type(namespace, name)

        if format_type == "image":
            obj = Image(namespace, name)
        elif format_type == "cube":
            obj = Cube(namespace, name)
        else:
            raise ValueError(f"Data type unknown: {format_type}")

        print(f"Data type of {namespace}:{name} detected as: {format_type}")

        return obj

