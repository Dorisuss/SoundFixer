#!/usr/bin/env bash
# Сборка soundfixer/bin/deepfilter.dll из исходников DeepFilterNet (Linux → Windows, через mingw).
# Нужны: rustup (stable) и mingw-w64:  apt install gcc-mingw-w64-x86-64
set -euo pipefail
REV=d375b2d8309e0935d165700c91da9de862a99c31   # DeepFilterNet main, 17.10.2024
WORK=${1:-/tmp/deepfilter-build}
rm -rf "$WORK" && git clone https://github.com/Rikorose/DeepFilterNet.git "$WORK"
cd "$WORK" && git checkout "$REV"
# C-интерфейс без встроенной модели: модель грузится из файла (soundfixer/models)
sed -i 's/^capi = \["tract", "default-model", "dep:ndarray", "logging"\]/capi = ["tract", "dep:ndarray", "logging"]/' libDF/Cargo.toml
rustup target add x86_64-pc-windows-gnu
cd libDF
cargo build --profile release-lto --target x86_64-pc-windows-gnu --no-default-features --features capi
cp "$WORK/target/x86_64-pc-windows-gnu/release-lto/df.dll" "$(dirname "$0")/../soundfixer/bin/deepfilter.dll" 2>/dev/null \
  || echo "Готово: $WORK/target/x86_64-pc-windows-gnu/release-lto/df.dll"
