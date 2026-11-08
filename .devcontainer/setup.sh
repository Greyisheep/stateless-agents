#!/usr/bin/env bash
# Installs pinned kind, kubectl and uv from direct download links, then starts the demo.
# Pinned on purpose: the devcontainer features that install "latest" ask GitHub's API for
# the version without logging in, and Codespaces share addresses, so a room full of people
# opening this at once gets "403: rate limit exceeded" and a codespace in recovery mode.
set -euo pipefail
KIND=v0.33.0 KUBECTL=v1.33.9 UV=0.8.19
case "$(uname -m)" in
  x86_64) arch=amd64 uvarch=x86_64 ;;
  aarch64|arm64) arch=arm64 uvarch=aarch64 ;;
  *) echo "unsupported machine: $(uname -m)" >&2; exit 1 ;;
esac
get() { curl -fsSL --retry 5 --retry-delay 3 "$1" -o "$2"; }
tmp=$(mktemp -d)
get "https://github.com/kubernetes-sigs/kind/releases/download/$KIND/kind-linux-$arch" "$tmp/kind"
get "https://dl.k8s.io/release/$KUBECTL/bin/linux/$arch/kubectl" "$tmp/kubectl"
get "https://github.com/astral-sh/uv/releases/download/$UV/uv-$uvarch-unknown-linux-gnu.tar.gz" "$tmp/uv.tgz"
tar -xzf "$tmp/uv.tgz" -C "$tmp" --strip-components=1
sudo install -m 0755 "$tmp/kind" "$tmp/kubectl" "$tmp/uv" "$tmp/uvx" /usr/local/bin/
rm -rf "$tmp"
make up
