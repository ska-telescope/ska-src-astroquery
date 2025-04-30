from astropy import units as u
from astropy.coordinates import SkyCoord
from .core import SRCNet

### Base class
class SKAProduct:

    def __init__(self, namespace=None, name=None):
        self.namespace = namespace
        self.name = name

    def show_methods(self):
        """ Show the methods for a class. """
        print(f"\nAvailable Format Factory methods for {self.__class__.__name__}:")
        for method in self.list_methods():
            print(f"- {method}")

    def list_methods():
        return []

    def subcube(self, circle=None, range_=None, polygon=None, output_file=None, **kwargs):
        """ Call the SODA service to save a subset of the dataset locally. """
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
        """ Read the Rucio Obscore metadata field from the dataset """
        metadata = SRCNet.get_metadata(self.namespace, self.name)
        print(f"\nDisplaying metadata for {self.namespace}:{self.name}")
        print("-" * 60)
        for key, value in metadata.items():
            print(f"{key:20}: {value}")
        print("-" * 60)

    def query_region(self, ra=None, dec=None, radius=None, width=None, height=None,
                     columns='*', row_limit=100, sync=True):
        """ Query region from s_ra and s_dec in loaded dataset metadata """
        try:
            if ra is None or dec is None:
                metadata = SRCNet.get_metadata(self.namespace, self.name)
                ra = metadata.get('s_ra')
                dec = metadata.get('s_dec')
                radius = metadata.get('s_fov', radius)

                if ra is None or dec is None:
                    raise ValueError("ERROR: s_ra and/or s_dec not available in metadata.")
                print(f"Using coordinates: RA={ra}, DEC={dec}, radius={radius}")

            coordinates = SkyCoord(ra=float(ra) * u.deg, dec=float(dec) * u.deg)
            radius_deg = float(radius)

            results = SRCNet.query_region(
                coordinates=coordinates,
                radius=radius_deg,
                width=width,
                height=height,
                columns=columns,
                row_limit=row_limit,
                sync=sync
            )
            print(f"\n Query region around RA={coordinates.ra.deg}, DEC={coordinates.dec.deg}:")
            print(results)
            return results

        except Exception as e:
            print(f"ERROR: Query region failed: {e}")
            return None

### Data types
class Image(SKAProduct):
    @staticmethod
    def list_methods():
        return [
            "subcube",
            "show_metadata",
            "query_region"
            "fits_header_info (placeholder)",
        ]

class Cube(SKAProduct):
    @staticmethod
    def list_methods():
        return [
            "subcube",
            "show_metadata",
            "query_region",
            "fits_header_info (placeholder)",
        ]

class Spectra(SKAProduct):
    @staticmethod
    def list_methods():
        return [
            "show_metadata",
            "fit_gaussian (placeholder)",
        ]

class Visibility(SKAProduct):
    @staticmethod
    def list_methods():
        return [
            "show_metadata",
            "plot_uv_coverage (placeholer)",
        ]

### Map dataproduct_type strings from Rucio metadata to Python code classes
TYPE_MAP = {
    "image": Image,
    "cube": Cube,
    "spectra": Spectra,
    "visibility": Visibility
}

class SKAFormatFactory:
    @staticmethod
    def get_format_type(namespace, name):
        metadata = SRCNet.get_metadata(namespace, name)
        #print("DEBUG: Metadata received:", metadata)
        return metadata.get("dataproduct_type", "unknown")

    @staticmethod
    def get(namespace, name):
        format_type = SKAFormatFactory.get_format_type(namespace, name)
        product_class = TYPE_MAP.get(format_type)
        if not product_class:
            raise ValueError(f"Data type unknown: {format_type}")

        obj = product_class(namespace, name)
        print(f"Data type of {namespace}:{name} detected as: {format_type}")
        obj.format_type = format_type
        return obj

    def list_methods_for_type(format_type):
        product_class = TYPE_MAP.get(format_type)
        if product_class:
            product_class.show_methods()
        else:
            print(f"ERROR: No methods available for unknown type '{format_type}'")

    @staticmethod
    def get_class_by_type(format_type):
        return TYPE_MAP.get(format_type)

    @staticmethod
    def query_object(object_name, radius=0.1, width=None, height=None,
                     columns='*', row_limit=100, sync=True):
        """ Direct query using object name without loading a dataset first. """
        try:
            coordinates = SkyCoord.from_name(object_name, frame="icrs")
            results = SRCNet.query_region(
                coordinates=coordinates,
                radius=radius,
                width=width,
                height=height,
                columns=columns,
                row_limit=row_limit,
                sync=sync
            )
            print(f"\n Query object results for {object_name} (RA={coordinates.ra.deg}, DEC={coordinates.dec.deg}):")
            print(results)
            return results
        except Exception as e:
            print(f"ERROR: Failed to query object {object_name}: {e}")
            return None

    def query_region(ra, dec, radius=0.1, width=None, height=None,
                     columns='*', row_limit=100, sync=True):
        """ Direct query using RA/DEC without loading a dataset first. """
        try:
            coordinates = SkyCoord(ra=ra * u.deg, dec=dec * u.deg)
            radius_deg = float(radius)
 

            results = SRCNet.query_region(
                coordinates=coordinates,
                radius=radius_deg,
                width=width,
                height=height,
                columns=columns,
                row_limit=row_limit,
                sync=sync
            )
            print(f"\n Query results at RA={ra}, DEC={dec}:")
            print(results)
            return results
        except Exception as e:
            print(f"ERROR: Failed to query region: {e}")
            return None
