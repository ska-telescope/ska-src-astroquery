from .core import SRCNet
import pyvo
import os
import requests
from astropy.io import fits
from astroquery.alma import Alma
from astroquery.vo_conesearch import conesearch
from astropy.coordinates import SkyCoord
from astropy import units as u

### Base class
class SKAProduct:
    def __init__(self, namespace, name):
        self.namespace = namespace
        self.name = name

    ### Methods
    def show_methods(self):
        print(f"\nAvailable Format Factory methods for type: {self.format_type}")
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

    def show_metadata(self):
        metadata = SRCNet.get_metadata(self.namespace, self.name)
        print(f"\nDisplaying metadata for {self.namespace}:{self.name}")
        print("-" * 60)
        for key, value in metadata.items():
            print(f"{key:20}: {value}")
        print("-" * 60)

    def query_alma_coords(self):
        metadata = SRCNet.get_metadata(self.namespace, self.name)
        ra = metadata.get("s_ra")
        dec = metadata.get("s_dec")
        radius = metadata.get("s_fov")

        if ra is None or dec is None or radius is None:
            print("Error: s_ra/s_dec/s_fov not available in metadata")
            return
        try:
            coord = SkyCoord(ra=float(ra) * u.deg, dec=float(dec) * u.deg)
            alma = Alma()
            results = alma.query_region(coord, radius=float(radius) * u.deg)
            print("\nALMA Query results in the region:")
            print(results)
        except Exception as e:
            print(f"Failed to query ALMA region: {e}")

### Data type: Image
class Image(SKAProduct):
    def list_methods(self):
        return [
            "subcube",
            "show_metadata",
            "query_alma_coords",
            "display (placeholder)",
            "header_info (placeholder)",
            "fits_to_hdf5 (placeholder)"
        ]

### Data type: Cube
class Cube(SKAProduct):
    def list_methods(self):
        return [
            "subcube",
            "show_metadata",
            "query_alma_coords",
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
        elif format_type == "spectra":
            obj = Spectra(namespace, name)
        elif format_type == "visibility":
            obj = Visibility(namespace, name)
        else:
            raise ValueError(f"Data type unknown: {format_type}")

        print(f"Data type of {namespace}:{name} detected as: {format_type}")

        obj.format_type = format_type

        return obj

