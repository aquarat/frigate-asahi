FROM ghcr.io/blakeblackshear/frigate:stable-standard-arm64

# Patch asyncio.SubprocessError -> sp.SubprocessError
# asyncio has no SubprocessError attribute; subprocess is already imported as sp.
# This is fixed in the upstream source but not yet in the stable release image.
RUN sed -i 's/asyncio\.SubprocessError/sp.SubprocessError/g' \
        /opt/frigate/frigate/util/services.py \
    && find /opt/frigate/frigate/util/__pycache__ -name 'services.cpython-*.pyc' -delete 2>/dev/null || true
