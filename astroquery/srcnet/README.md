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

The examples here utilise the `srcnet.login()` function. It is advisable to keep hold of the `access_token` and `refresh_token` that result from this command due to a bug in v1.8.2 of 
Indigo IAM. This bug does not allow you to call this function twice within the access token's lifetime i.e. you will not be able to generate a second access token until the first one has 
expired. This has been patched in v1.8.3 of Indigo IAM.

The `access_token` and `refresh_token` can be passed to the `SRCNet()` constructor (`access_token=` and `refresh_token=` respectively) instead, circumventing the need to call this function 
more than once.

### query_region

```bash
$ python3 -c "from astroquery.srcnet import SRCNet; srcnet=SRCNet(); srcnet.login(); q=srcnet.query_region(coordinates='82.1deg 12.58deg', radius=0.01); print(q)"

<Table length=1>
dataproduct_type dataproduct_subtype calib_level obs_collection       obs_id      ... em_ucd preview  source_table         dist        
                                                                                  ...                                      deg         
     object             object          int16        object           object      ... object  object     object          float64       
---------------- ------------------- ----------- -------------- ----------------- ... ------ ------- ------------- --------------------
           image                               2           RACS RACS-DR1_0528+12A ...                rucio.obscore 0.009205321609571323
```

### query_object

```bash
$ python3 -c "from astroquery.srcnet import SRCNet; srcnet=SRCNet(); srcnet.login(); q=srcnet.query_object(object_name='PTF10tce', radius=0.01); print(q)"

<Table length=1>
dataproduct_type dataproduct_subtype calib_level   obs_collection   ... em_ucd preview  source_table          dist        
                                                                    ...                                       deg         
     object             object          int16          object       ... object  object     object           float64       
---------------- ------------------- ----------- ------------------ ... ------ ------- ------------- ---------------------
                                               1 collection_testing ...                rucio.obscore 7.176247592607064e-05
```

### get_data

```bash
 python3 -c "from astroquery.srcnet import SRCNet; srcnet=SRCNet(); srcnet.login(); q=srcnet.get_data(namespace='testing', name='PTF10tce.fits')"

DEBUG: Access token already exists for service, will not attempt token exchange [astroquery.srcnet.core]
DEBUG: Access token is valid, will not attempt token refresh. [astroquery.srcnet.core]
8248KB downloaded
```

# Notes

- Because the astroquery contributing guide states that external dependencies are not desirable, the client functionality that comes with each 
API (i.e. the calls to each API REST interface) has to be duplicated here.