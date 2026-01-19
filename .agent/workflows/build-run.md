---
description: Build and Run the Flatpak application
---

Para buildar e rodar o projeto em um único comando:

// turbo
1. Execute o build e instalação:
```bash
flatpak-builder --user --install --force-clean build-dir org.ziso.gui.yml
```

// turbo
2. Inicie o aplicativo:
```bash
flatpak run org.ziso.gui
```
