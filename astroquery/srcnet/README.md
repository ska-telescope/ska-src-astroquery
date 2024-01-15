# SRCNet

This is a scratch README.

## Authentication

For authenticated requests, there is a `login` function. 

## Install

Clone repository and install with package symlinks so you can change the code and run without reinstalling the package:

```bash
python3 -m pip install -e .
```

## From remote package registry

```
$ python3 -m pip install astroquery --index-url https://gitlab.com/api/v4/projects/53653803/packages/pypi/simple
```

## Development

### Package

The package will be created by the CI pipeline. If not incrementing the version, you must delete the existing package with the same version before commit otherwise the job will fail.

### Docs

First install `sphinx-astropy`:

```bash
$ python3 -m pip install sphinx-astropy
```

Then run the sphinx `html` Make target:

```bash
$ cd docs && make html
```

This will then be made into a GitLab page by the CI pipeline.

## Examples

### query_region

```bash
$ python3 -c "from astroquery.srcnet import SRCNet; srcnet=SRCNet(); q=srcnet.query_region(coordinates='82.1deg 12.58deg', radius=0.01); print(q)"
```

### query_object

```bash
$python3 -c "from astroquery.srcnet import SRCNet; srcnet=SRCNet(); q=srcnet.query_object(object_name='PTF10tce', radius=0.01); print(q)"
```

# Notes

- Because the astroquery contributing guide states that external dependencies are not desirable, the client functionality that comes with each 
API (i.e. the calls to each API REST interface) has to be duplicated here.

- `get_data` function is incomplete due to not having a download endpoint in the DM API.

- Due to a bug in IAM the `/login` endpoint can be called only once every 60 minutes so make sure to keep hold of your access token and refresh token between calls.