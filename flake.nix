{
  # pm: a project manager for Claude Code workers (README.md).
  #   nix run . -- status      nix build      nix develop
  #   nix run .#dashboard      the optional web view (dashboard/), not part of the default package
  # `claude` itself is not included: pm runs the one on your PATH.
  description = "A project manager for Claude Code workers";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in {
      packages = forAll (pkgs: rec {
        default = pkgs.python3Packages.buildPythonApplication {
          pname = "pm";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          makeWrapperArgs = [ "--prefix" "PATH" ":" "${pkgs.git}/bin" ];
          meta.mainProgram = "pm";
        };
        dashboard = pkgs.python3Packages.buildPythonApplication {
          pname = "pm-dashboard";
          version = "0.1.0";
          pyproject = true;
          src = ./dashboard;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = [ (pkgs.python3Packages.toPythonModule default) ];
          makeWrapperArgs = [ "--prefix" "PATH" ":" "${pkgs.git}/bin" ];
          meta.mainProgram = "pm-dashboard";
        };
      });

      apps = forAll (pkgs: {
        default = { type = "app"; program = "${self.packages.${pkgs.system}.default}/bin/pm"; };
        dashboard = { type = "app"; program = "${self.packages.${pkgs.system}.dashboard}/bin/pm-dashboard"; };
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell { packages = [ pkgs.python3 pkgs.git ]; };
      });
    };
}
