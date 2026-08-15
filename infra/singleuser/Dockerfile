# This local-development image deliberately contains two reviewed Python kernels.
# Production must replace the base and resulting profile image with digest-pinned
# artifacts from the company registry; the profile loader enforces that boundary.
ARG SINGLEUSER_BASE_IMAGE=quay.io/jupyterhub/singleuser@sha256:2c952ea9a86a81994b6a5fbd9651e3c55c63008068ad1a27e559c4112d7bcaca
FROM ${SINGLEUSER_BASE_IMAGE}

USER root

# The base interpreter/kernel is Python 3.13.14. Keep the additional environment
# separate so changing the selected notebook kernel never changes the Hub client.
RUN /opt/conda/bin/mamba create --yes --quiet \
      --prefix /opt/conda/envs/python312 \
      python=3.12.13 \
      ipykernel=7.3.0 \
 && /opt/conda/envs/python312/bin/python -m ipykernel install \
      --prefix=/opt/conda \
      --name=python312 \
      --display-name='Python 3.12 (platform)' \
 && /opt/conda/bin/mamba clean --all --yes \
 && find /opt/conda/envs/python312 -type d -exec chmod a+rx {} +

COPY --chmod=0755 platform-singleuser /usr/local/bin/platform-singleuser
COPY --chmod=0755 verify_kernel_contract.py /usr/local/libexec/verify_kernel_contract.py
COPY --chmod=0755 verify_private_storage.py /usr/local/libexec/verify_private_storage.py
COPY --chmod=0755 verify_shared_storage.py /usr/local/libexec/verify_shared_storage.py
# Profile selection intentionally puts another Python environment first on PATH.
# Pin the inherited notebook healthcheck to the base interpreter that contains
# its `requests` dependency instead of resolving `python3` through that PATH.
HEALTHCHECK --interval=3s --timeout=1s --start-period=3s --retries=3 \
  CMD /opt/conda/bin/python /etc/jupyter/docker_healthcheck.py || exit 1
RUN chmod 0755 /usr/local/libexec \
 && PLATFORM_DEFAULT_KERNEL=python312 \
    PLATFORM_PYTHON_EXECUTABLE=/opt/conda/envs/python312/bin/python \
    PATH=/opt/conda/envs/python312/bin:/opt/conda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PLATFORM_KERNEL_CONTRACT='{"schema_version":1,"python_version":"3.12.13","kernels":[{"name":"python3","display_name":"Python 3 (ipykernel)","language":"python","python_version":"3.13.14","executable":"/opt/conda/bin/python"},{"name":"python312","display_name":"Python 3.12 (platform)","language":"python","python_version":"3.12.13","executable":"/opt/conda/envs/python312/bin/python"}],"default_kernel":"python312"}' \
    /opt/conda/bin/python /usr/local/libexec/verify_kernel_contract.py \
 && PLATFORM_DEFAULT_KERNEL=python3 \
    PLATFORM_PYTHON_EXECUTABLE=/opt/conda/bin/python \
    PATH=/opt/conda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PLATFORM_KERNEL_CONTRACT='{"schema_version":1,"python_version":"3.13.14","kernels":[{"name":"python3","display_name":"Python 3 (ipykernel)","language":"python","python_version":"3.13.14","executable":"/opt/conda/bin/python"},{"name":"python312","display_name":"Python 3.12 (platform)","language":"python","python_version":"3.12.13","executable":"/opt/conda/envs/python312/bin/python"}],"default_kernel":"python3"}' \
    /opt/conda/bin/python /usr/local/libexec/verify_kernel_contract.py

USER 1000:100
