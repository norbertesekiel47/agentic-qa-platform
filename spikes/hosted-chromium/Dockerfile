# The spike's trial, one package per candidate (ADR-0008 amendment,
# 2026-09-30). package.sh builds it from a context holding only what the
# trial installs, taken from HEAD:
#   docker build --platform linux/arm64 --target fargate|lambda|microvm <context>
#
# Lambda builds a MicroVM image from a zip of the same context, with no
# target, so the MicroVM's stage comes last. All three stages share one base:
# the container base Lambda publishes for MicroVMs (Amazon Linux 2023, arm64
# only), so the candidates differ only in their platform.
# https://docs.aws.amazon.com/lambda/latest/dg/microvms-images.html#microvms-images-container-base
FROM public.ecr.aws/lambda/microvms:al2023-minimal@sha256:05cb9b38d841e7ff1b693dc9e894909612f340bf99ec97d426e8000a5bbe96c3 AS trial
COPY --from=ghcr.io/astral-sh/uv:0.11.15@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3 /uv /usr/local/bin/uv

# The libraries Playwright's headless shell links against, as AL2023 names
# them (`playwright install --with-deps` knows only Debian and Ubuntu), and
# useradd. The build fails below if the shell still lacks one.
RUN dnf install -y --setopt=install_weak_deps=0 \
        alsa-lib at-spi2-core atk dbus-libs expat libX11 libXcomposite \
        libXdamage libXext libXfixes libXrandr libxcb libxkbcommon mesa-libgbm \
        nspr nss nss-util shadow-utils systemd-libs \
    && dnf clean all

# Python 3.14 (.python-version) comes from uv, since AL2023 doesn't ship it.
# HOME is /tmp, the one directory a Lambda function may write.
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/trial \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PATH=/opt/trial/bin:$PATH \
    HOME=/tmp

# uv.lock's versions, installed as wheels, so the sources go once they are.
# The check covers the libraries the shell loads at run time (EGL, GLES,
# SwiftShader) as well as the shell itself.
COPY . /build
RUN cd /build \
    && uv sync --locked --no-dev --no-editable --package aqa-hosted-chromium-spike \
    && playwright install --only-shell chromium \
    && cd /opt/ms-playwright/chromium_headless_shell-*/*/ \
    && ldd chrome-headless-shell *.so* > /tmp/ldd.txt \
    && ! grep 'not found' /tmp/ldd.txt \
    && rm -rf /build /tmp/ldd.txt /root/.cache

# Chromium won't sandbox a browser that runs as root.
RUN useradd --system --no-create-home trial
USER trial

FROM trial AS fargate
CMD ["python", "-m", "aqa_hosted_chromium_spike"]

# A Lambda function runs a non-AWS image through the runtime interface client.
# https://github.com/aws/aws-lambda-python-runtime-interface-client#usage
FROM trial AS lambda
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["aqa_hosted_chromium_spike.lambda_function.handler"]

FROM trial AS microvm
EXPOSE 8080
CMD ["python", "-m", "aqa_hosted_chromium_spike.microvm"]
