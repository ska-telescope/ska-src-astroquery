from astroquery.srcnet import SRCNet

# Get the SKA IAM token
SRCNet.login()

# Example: Perform a SODA cutout
# Note: In this example, you need access to the 'data/namespaces/testing' SKA-IAM group
#
cutout = SRCNet.soda_cutout(
    soda_url="https://gatekeeper.srcdev.skao.int/soda/ska/datasets/soda",
    dataset_path="testing/5b/f5",
    file_name="PTF10tce.fits",
    filtering_parameter="CIRCLE",
    ra=351.986728,
    dec=8.778684,
    radius=0.1,
    output_path="output",
    output_filename="soda-cutout-test.fits"
)
