from astropy import units as u
from astropy.coordinates import SkyCoord
from .core import SRCNet


### Base class
class SKAProduct:

    def __init__(self, namespace=None, name=None):
        self.namespace = namespace
        self.name = name

    def show_methods(self):
        print(f"\nAvailable methods for {self.__class__.__name__}:")
        for method in self.list_methods():
            print(f"- {method}")

    @staticmethod
    def list_methods():
        return []

    def cutout(self, *args, **kwargs):
        raise NotImplementedError("Not implemented in SKAProduct base class.")

    def subcube(self, *args, **kwargs):
        raise NotImplementedError("Not implemented in SKAProduct base class.")

    def show_metadata(self):
        metadata = SRCNet.get_metadata(self.namespace, self.name)
        print(f"\nMetadata for {self.namespace}:{self.name}")
        for key, value in metadata.items():
            print(f"{key:20}: {value}")

    def fits_header_info(self):
        print("fits_header_info method is not yet implemented.")

### Dataproduct type classes
class Image(SKAProduct):

    @staticmethod
    def list_methods():
        return ["subcube", 
                "show_metadata", 
                "fits_header_info (placeholder)"
        ]

    def cutout(self, circle=None, range_=None, polygon=None, output_file=None, **kwargs):
        return SRCNet.soda_cutout(
            namespace=self.namespace,
            name=self.name,
            circle=circle,
            polygon=polygon,
            range_=range_,
            output_file=output_file,
            **kwargs
        )

class Cube(SKAProduct):

    @staticmethod
    def list_methods():
        return ["subcube", 
                "show_metadata", 
                "fits_header_info (placeholder)"
        ]

    def subcube(self, circle=None, range_=None, polygon=None, output_file=None, **kwargs):
        return SRCNet.soda_cutout(
            namespace=self.namespace,
            name=self.name,
            circle=circle,
            polygon=polygon,
            range_=range_,
            output_file=output_file,
            **kwargs
        )

class Spectra(SKAProduct):

    @staticmethod
    def list_methods():
        return ["show_metadata", 
                "fit_gaussian (placeholder)"
        ]

class Visibility(SKAProduct):

    @staticmethod
    def list_methods():
        return ["show_metadata", 
                "plot_uv_coverage (placeholder)"
        ]

### Format Factory class
class SKAFormatFactory:

    ### Map lowercase dataproduct_type strings from Rucio metadata to Python code classes
    TYPE_MAP = {
        "image": Image,
        "cube": Cube,
        "spectra": Spectra,
        "visibility": Visibility
    }

    @staticmethod
    def get_format_type(namespace, name):
        metadata = SRCNet.get_metadata(namespace, name)
        return metadata.get("dataproduct_type", "unknown")

    @staticmethod
    def get(namespace, name):
        format_type = SKAFormatFactory.get_format_type(namespace, name)
        product_class = SKAFormatFactory.TYPE_MAP.get(format_type)
        if not product_class:
            raise ValueError(f"Dataproduct type unknown: {format_type}")

        obj = product_class(namespace, name)
        print(f"Detected data type of {namespace}:{name}: {format_type}")
        return obj

    @staticmethod
    def list_methods_for_type(format_type):
        product_class = SKAFormatFactory.TYPE_MAP.get(format_type)
        if product_class:
            methods = product_class.list_methods()
            print(f"\nAvailable methods for {format_type}:")
            for method in methods:
                print(f"- {method}")
        else:
            print(f"No methods available for type '{format_type}'")

    @staticmethod
    def get_class_by_type(format_type):
        return SKAFormatFactory.TYPE_MAP.get(format_type)

