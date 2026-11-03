{
  description = "VisualTester-Esp32: VisualTester device plugin for ESP32 boards";

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
          name = "visualtester-esp32";
          runtimeInputs = [ python pkgs.esptool ];
          text = ''
            exec python3 ${src}/esp32_plugin.py "$@"
          '';
        };

        tests = pkgs.writeShellApplication {
          name = "visualtester-esp32-tests";
          runtimeInputs = [ python ];
          text = ''
            cd ${src}
            exec python3 -m unittest discover -s tests -v "$@"
          '';
        };
      in {
        packages.default = plugin;
        packages.visualtester-esp32 = plugin;

        # nix run            -> the plugin on :8091
        # nix run .#tests    -> unit tests against the fake board
        apps.default = { type = "app"; program = "${plugin}/bin/visualtester-esp32"; };
        apps.tests = { type = "app"; program = "${tests}/bin/visualtester-esp32-tests"; };

        devShells.default = pkgs.mkShell { packages = [ python pkgs.esptool ]; };
      });
}
