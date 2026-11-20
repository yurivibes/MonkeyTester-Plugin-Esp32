{
  description = "MonkeyTester-Esp32-Sensor: MonkeyTester device plugin for the ESP32 sensor boards";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ] (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python3.withPackages (ps: [ ps.pyserial ]);
        src = pkgs.lib.cleanSource ./.;

        plugin = pkgs.writeShellApplication {
          name = "monkeytester-esp32-sensor";
          runtimeInputs = [ python pkgs.esptool ];
          text = ''
            export PLUGIN_PORT="''${PLUGIN_PORT:-8091}"
            exec python3 ${src}/esp32_plugin.py "$@"
          '';
        };

        tests = pkgs.writeShellApplication {
          name = "monkeytester-esp32-sensor-tests";
          runtimeInputs = [ python ];
          text = ''
            cd ${src}
            exec python3 -m unittest discover -s tests -v "$@"
          '';
        };
      in {
        packages.default = plugin;
        packages.monkeytester-esp32-sensor = plugin;

        # nix run .#      -> esp32-sensor plugin on :8091
        # nix run .#tests -> unit tests against the fake boards
        apps.default = { type = "app"; program = "${plugin}/bin/monkeytester-esp32-sensor"; };
        apps.tests = { type = "app"; program = "${tests}/bin/monkeytester-esp32-sensor-tests"; };

        devShells.default = pkgs.mkShell { packages = [ python pkgs.esptool ]; };
      });
}
