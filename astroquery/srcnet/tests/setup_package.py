import os
import subprocess
import sys

def setup_package():
    """Generate a version.py for the unit tests to run because pytest reads 
    astroquery/conftest.py and that requires a version.py to be present.
    (Alternatively run "pip install ." in the ska-src-astroquery directory to generate one)
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../'))
    version_dir = os.path.join(repo_root, 'astroquery')
    version_path = os.path.join(version_dir, 'version.py')

    print(f"Writing version.py to: {version_path}")

    with open(version_path, 'w') as f:
        f.write("version = '0.0'\n")
        f.write("astropy_helpers_version = '0.0'\n")

def install_requirements():
    """Install required pip packages from requirements.txt"""
    script_dir = os.path.dirname(__file__)
    requirements_path = os.path.join(script_dir, 'requirements.txt')

    print(f"Installing packages in {requirements_path}...")

    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", requirements_path])

if __name__ == '__main__':
    setup_package()
    install_requirements()
