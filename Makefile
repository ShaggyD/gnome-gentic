UUID = harness-usage@dchadwick.local
DEST = $(HOME)/.local/share/gnome-shell/extensions/$(UUID)

install:
	mkdir -p $(DEST)/icons
	cp $(UUID)/extension.js $(UUID)/metadata.json $(UUID)/stylesheet.css $(UUID)/collector.py $(DEST)/
	cp $(UUID)/icons/*.png $(DEST)/icons/
	@echo "Installed. Restart GNOME Shell (log out/in on Wayland), then:"
	@echo "  gnome-extensions enable $(UUID)"

uninstall:
	rm -rf $(DEST)
	@echo "Removed. Restart GNOME Shell."

pack:
	gnome-extensions pack --force --extra-source=icons $(UUID)

check:
	python3 $(UUID)/collector.py --self-test
	python3 $(UUID)/collector.py | python3 -m json.tool > /dev/null
	@echo "collector OK"

.PHONY: install uninstall pack check
