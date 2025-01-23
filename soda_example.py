from astroquery.srcnet import SRCNet

# Get the SKA IAM token
SRCNet.login()

# Example: Perform a SODA cutout
# Note: In this example, you need access to the 'data/namespaces/testing' SKA-IAM group
#
cutout = SRCNet.soda_cutout(
    namespace="testing",
    name="PTF10tce.fits",
    circle=(351.986728, 8.778684, 0.1),
    output_file="output/soda-cutout-test.fits"
)
