APP_ID = org.ziso.gui
MANIFEST = org.ziso.gui.yml
BUILD_DIR = build-dir
REPO_DIR = repo
BUNDLE = org.ziso.gui.flatpak

.PHONY: all build run clean bundle

all: build run

build:
	flatpak-builder --user --install --force-clean $(BUILD_DIR) $(MANIFEST)

bundle:
	flatpak-builder --repo=$(REPO_DIR) --force-clean $(BUILD_DIR) $(MANIFEST)
	flatpak build-bundle $(REPO_DIR) $(BUNDLE) $(APP_ID)

run:
	flatpak run $(APP_ID)

clean:
	rm -rf $(BUILD_DIR) .flatpak-builder locale $(REPO_DIR) $(BUNDLE)
