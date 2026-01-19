APP_ID = org.ziso.gui
MANIFEST = org.ziso.gui.yml
BUILD_DIR = build-dir

.PHONY: all build run clean

all: build run

build:
	flatpak-builder --user --install --force-clean $(BUILD_DIR) $(MANIFEST)

run:
	flatpak run $(APP_ID)

clean:
	rm -rf $(BUILD_DIR) .flatpak-builder
