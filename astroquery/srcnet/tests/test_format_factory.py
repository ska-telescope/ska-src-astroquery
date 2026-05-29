"""
Tests for astroquery.srcnet.format_factory module.

Covers:
  - SKAProduct base class: init, show_methods, cutout/subcube stubs
  - Image, Cube, Spectra, Visibility product classes: list_methods, delegates
  - SKAFormatFactory: get, get_class_by_type, list_methods_for_type
"""
import pytest
from unittest.mock import patch

from astroquery.srcnet.format_factory import (
    Cube,
    Image,
    SKAFormatFactory,
    SKAProduct,
    Spectra,
    Visibility,
)


# ─────────────────────────────────────────────────────────────────────────────
# SKAProduct base class
# ─────────────────────────────────────────────────────────────────────────────

class TestSKAProduct:
    """Base product: stores identity, lists no methods, stubs out IO ops."""

    def test_init_stores_namespace_and_name(self):
        p = SKAProduct("my_ns", "my_file.fits")
        assert p.namespace == "my_ns"
        assert p.name == "my_file.fits"

    def test_init_defaults_to_none(self):
        p = SKAProduct()
        assert p.namespace is None
        assert p.name is None

    def test_list_methods_returns_empty_for_base(self):
        assert SKAProduct.list_methods() == []

    def test_show_methods_prints_class_name(self, capsys):
        p = Image("ns", "f")
        p.show_methods()
        out = capsys.readouterr().out
        assert "Image" in out

    def test_cutout_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            SKAProduct("ns", "f").cutout()

    def test_subcube_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            SKAProduct("ns", "f").subcube()


# ─────────────────────────────────────────────────────────────────────────────
# Image
# ─────────────────────────────────────────────────────────────────────────────

class TestImageProduct:
    def test_list_methods_not_empty(self):
        assert len(Image.list_methods()) > 0

    def test_cutout_delegates_to_soda(self):
        img = Image("ns", "myimage.fits")
        with patch("astroquery.srcnet.format_factory.SRCNet.soda_cutout") as mock_soda:
            img.cutout(circle=(1, 2, 0.1))
        mock_soda.assert_called_once()

    def test_cutout_passes_namespace_and_name(self):
        img = Image("my_ns", "my_name")
        with patch("astroquery.srcnet.format_factory.SRCNet.soda_cutout") as mock_soda:
            img.cutout()
        call_kwargs = mock_soda.call_args[1]
        assert call_kwargs["namespace"] == "my_ns"
        assert call_kwargs["name"] == "my_name"


# ─────────────────────────────────────────────────────────────────────────────
# Cube
# ─────────────────────────────────────────────────────────────────────────────

class TestCubeProduct:
    def test_list_methods_not_empty(self):
        assert len(Cube.list_methods()) > 0

    def test_subcube_delegates_to_soda(self):
        cube = Cube("ns", "mycube.fits")
        with patch("astroquery.srcnet.format_factory.SRCNet.soda_cutout") as mock_soda:
            cube.subcube(range_=(1e9, 2e9))
        mock_soda.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Spectra and Visibility (placeholder products)
# ─────────────────────────────────────────────────────────────────────────────

class TestSpectraProduct:
    def test_list_methods_includes_show_metadata(self):
        assert any("show_metadata" in m for m in Spectra.list_methods())

    def test_instantiates_as_ska_product(self):
        s = Spectra("ns", "spec.fits")
        assert isinstance(s, SKAProduct)


class TestVisibilityProduct:
    def test_list_methods_not_empty(self):
        assert len(Visibility.list_methods()) > 0

    def test_instantiates_as_ska_product(self):
        v = Visibility("ns", "vis.fits")
        assert isinstance(v, SKAProduct)


# ─────────────────────────────────────────────────────────────────────────────
# SKAFormatFactory
# ─────────────────────────────────────────────────────────────────────────────

class TestSKAFormatFactory:
    """Factory: maps dataproduct_type strings to product instances."""

    def _mock_metadata(self, dtype):
        return patch(
            "astroquery.srcnet.format_factory.SRCNet.get_metadata",
            return_value={"dataproduct_type": dtype},
        )

    def test_get_returns_image_for_image_type(self):
        with self._mock_metadata("image"):
            result = SKAFormatFactory.get("ns", "f")
        assert isinstance(result, Image)

    def test_get_returns_cube_for_cube_type(self):
        with self._mock_metadata("cube"):
            result = SKAFormatFactory.get("ns", "f")
        assert isinstance(result, Cube)

    def test_get_returns_spectra_for_spectra_type(self):
        with self._mock_metadata("spectra"):
            result = SKAFormatFactory.get("ns", "f")
        assert isinstance(result, Spectra)

    def test_get_returns_visibility_for_visibility_type(self):
        with self._mock_metadata("visibility"):
            result = SKAFormatFactory.get("ns", "f")
        assert isinstance(result, Visibility)

    def test_get_raises_for_unknown_type(self):
        with self._mock_metadata("unknown_xyz"):
            with pytest.raises(ValueError, match="unknown"):
                SKAFormatFactory.get("ns", "f")

    def test_get_prints_detected_type(self, capsys):
        with self._mock_metadata("image"):
            SKAFormatFactory.get("ns", "f")
        out = capsys.readouterr().out
        assert "image" in out

    def test_get_class_by_type_image(self):
        assert SKAFormatFactory.get_class_by_type("image") is Image

    def test_get_class_by_type_cube(self):
        assert SKAFormatFactory.get_class_by_type("cube") is Cube

    def test_get_class_by_type_spectra(self):
        assert SKAFormatFactory.get_class_by_type("spectra") is Spectra

    def test_get_class_by_type_visibility(self):
        assert SKAFormatFactory.get_class_by_type("visibility") is Visibility

    def test_get_class_by_type_unknown_returns_none(self):
        assert SKAFormatFactory.get_class_by_type("unknown") is None

    def test_list_methods_for_type_prints_methods(self, capsys):
        SKAFormatFactory.list_methods_for_type("image")
        out = capsys.readouterr().out
        assert "image" in out

    def test_list_methods_for_unknown_type_prints_no_methods(self, capsys):
        SKAFormatFactory.list_methods_for_type("unknowntype")
        out = capsys.readouterr().out
        assert "No methods" in out
