# SRCNet

This is a scratch README.

## Install

## Development

Clone repository and install with package symlinks so you can change the code and run without reinstalling the package:

```bash
python3 -m pip install -e .
```

## From remote package registry

```
pip install astroquery --index-url https://gitlab.com/api/v4/projects/53653803/packages/pypi/simple
```

## Examples

### query_region

```bash
python3 -c "from astroquery.srcnet import SRCNet; srcnet=SRCNet(); q=srcnet.query_region(coordinates='82.1deg 12.58deg', radius=0.01); print(q)"
```

### query_object

```bash
python3 -c "from astroquery.srcnet import SRCNet; srcnet=SRCNet(); q=srcnet.query_object(object_name='PTF10tce', radius=0.01); print(q)"
```