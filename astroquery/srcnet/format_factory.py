"""
SKA data-product format factory.

Maps the ``dataproduct_type`` string stored in Rucio metadata to a typed
Python object that exposes only the operations meaningful for that product.

Supported types
---------------
``image``       → :class:`Image`      — 2-D sky image; supports ``cutout()``
``cube``        → :class:`Cube`       — 3-D spectral cube; supports ``subcube()``
``spectra``     → :class:`Spectra`    — 1-D spectrum
``visibility``  → :class:`Visibility` — interferometric visibility data

Usage::

    from astroquery.srcnet.format_factory import SKAFormatFactory

    product = SKAFormatFactory.get("my_namespace", "my_file.fits")
    # Returns an Image/Cube/Spectra/Visibility instance depending on metadata
    product.show_metadata()
"""
from .core import SRCNet


class SKAProduct:
    """Abstract base class for all SRCNet data products.

    Concrete subclasses override :meth:`list_methods`, :meth:`cutout`, and
    :meth:`subcube` as appropriate for their data type.  Methods that are
    not yet implemented in a subclass delegate here and raise
    ``NotImplementedError``.

    Parameters
    ----------
    namespace : str, optional
        Rucio namespace (scope) of the data product.
    name : str, optional
        Rucio name (file identifier) of the data product.
    """

    def __init__(self, namespace=None, name=None):
        self.namespace = namespace
        self.name = name

    def show_methods(self):
        """Print the list of operations available for this product type."""
        print(f"\nAvailable methods for {self.__class__.__name__}:")
        for method in self.list_methods():
            print(f"- {method}")

    @staticmethod
    def list_methods():
        """Return the names of callable operations for this product type."""
        return []

    def cutout(self, *args, **kwargs):
        """Extract a spatial cutout — not implemented in the base class."""
        raise NotImplementedError("Not implemented in SKAProduct base class.")

    def subcube(self, *args, **kwargs):
        """Extract a spectral sub-cube — not implemented in the base class."""
        raise NotImplementedError("Not implemented in SKAProduct base class.")

    def show_metadata(self):
        """Fetch and print all Rucio metadata fields for this product."""
        metadata = SRCNet.get_metadata(self.namespace, self.name)
        print(f"\nMetadata for {self.namespace}:{self.name}")
        for key, value in metadata.items():
            print(f"{key:20}: {value}")

    def fits_header_info(self):
        """Placeholder — FITS header introspection is not yet implemented."""
        print("fits_header_info method is not yet implemented.")


class Image(SKAProduct):
    """2-D sky image product.

    Supports spatial cutouts via :meth:`cutout`, delegated to
    ``SRCNet.soda_cutout``.
    """

    @staticmethod
    def list_methods():
        """Return the names of the operations supported by this product type."""
        return ["cutout",
                "show_metadata",
                "fits_header_info (placeholder)"
        ]

    def cutout(self, circle=None, range_=None, polygon=None, output_file=None, **kwargs):
        """Request a SODA spatial cutout and write the result to *output_file*."""
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
    """3-D spectral-line cube product.

    Supports sub-cube extraction via :meth:`subcube`, delegated to
    ``SRCNet.soda_cutout`` with spectral-axis parameters.
    """

    @staticmethod
    def list_methods():
        """Return the names of the operations supported by this product type."""
        return ["subcube",
                "show_metadata",
                "fits_header_info (placeholder)"
        ]

    def subcube(self, circle=None, range_=None, polygon=None, output_file=None, **kwargs):
        """Request a SODA spatial+spectral sub-cube and write the result to *output_file*."""
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
    """1-D spectrum product.

    Gaussian fitting (``fit_gaussian``) is planned but not yet implemented.
    """

    @staticmethod
    def list_methods():
        """Return the names of the operations supported by this product type."""
        return ["show_metadata",
                "fit_gaussian (placeholder)"
        ]


class Visibility(SKAProduct):
    """Interferometric visibility (UV) data product.

    UV-coverage plotting (``plot_uv_coverage``) is planned but not yet
    implemented.
    """

    @staticmethod
    def list_methods():
        """Return the names of the operations supported by this product type."""
        return ["show_metadata",
                "plot_uv_coverage (placeholder)"
        ]


class SKAFormatFactory:
    """Factory that resolves a Rucio file to its typed product object.

    Queries the Rucio metadata for ``dataproduct_type``, maps it to the
    appropriate :class:`SKAProduct` subclass, and returns an instance
    initialised with the given namespace and name.

    Raises ``ValueError`` for unrecognised product types so the caller gets
    a clear error rather than a silent ``None``.
    """

    # Maps lowercase dataproduct_type strings (from Rucio metadata) to classes.
    TYPE_MAP = {
        "image":      Image,
        "cube":       Cube,
        "spectra":    Spectra,
        "visibility": Visibility,
    }

    @staticmethod
    def get_format_type(namespace, name):
        """Look up the ``dataproduct_type`` for a Rucio file."""
        metadata = SRCNet.get_metadata(namespace, name)
        return metadata.get("dataproduct_type", "unknown")

    @staticmethod
    def get(namespace, name):
        """Return a typed product object for the given Rucio file.

        Parameters
        ----------
        namespace : str
            Rucio scope.
        name : str
            Rucio file identifier.

        Returns
        -------
        :class:`SKAProduct`
            An ``Image``, ``Cube``, ``Spectra``, or ``Visibility`` instance.

        Raises
        ------
        ValueError
            If the ``dataproduct_type`` is not in :attr:`TYPE_MAP`.
        """
        format_type = SKAFormatFactory.get_format_type(namespace, name)
        product_class = SKAFormatFactory.TYPE_MAP.get(format_type)
        if not product_class:
            raise ValueError(f"Dataproduct type unknown: {format_type}")

        obj = product_class(namespace, name)
        print(f"Detected data type of {namespace}:{name}: {format_type}")
        return obj

    @staticmethod
    def list_methods_for_type(format_type):
        """Print available methods for a product type without constructing an instance."""
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
        """Return the product class for *format_type*, or ``None`` if unknown."""
        return SKAFormatFactory.TYPE_MAP.get(format_type)
