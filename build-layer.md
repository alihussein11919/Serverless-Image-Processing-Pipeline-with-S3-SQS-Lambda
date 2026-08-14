# Building the Pillow Lambda Layer

Pillow contains compiled C extensions, so it must be built for Lambda's
Linux runtime — a local `pip install` on Windows/macOS will not work when
uploaded directly.

```bash
mkdir -p pillow-layer/python
pip install Pillow==11.0.0 \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --target pillow-layer/python/

cd pillow-layer
zip -r9 pillow-layer.zip python
```

Upload `pillow-layer.zip` under Lambda → Layers → Create layer, with
compatible runtime Python 3.12 and compatible architecture x86_64 (match
whatever architecture your function uses — rebuild with
`manylinux2014_aarch64` instead if your function is arm64/Graviton).
