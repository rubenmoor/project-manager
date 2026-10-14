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
      # VERSION in pm/__init__.py, the one version of pm and the dashboard
      version = builtins.head (builtins.concatMap (l: let m = builtins.match "VERSION = ([0-9]+).*" l; in
        if m == null then [ ] else m) (nixpkgs.lib.splitString "\n" (builtins.readFile ./pm/__init__.py)));
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in {
      packages = forAll (pkgs: rec {
        default = pkgs.python3Packages.buildPythonApplication {
          pname = "pm";
          inherit version;
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          makeWrapperArgs = [ "--prefix" "PATH" ":" "${pkgs.git}/bin" ];
          meta.mainProgram = "pm";
        };
        dashboard = pkgs.python3Packages.buildPythonApplication {
          pname = "pm-dashboard";
          inherit version;
          pyproject = true;
          src = ./dashboard;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = [ (pkgs.python3Packages.toPythonModule default) ];
          # PYTHONPATH would come first: an older pm on it (a checkout) would shadow the pm built here
          makeWrapperArgs = [ "--prefix" "PATH" ":" "${pkgs.git}/bin" "--unset" "PYTHONPATH" ];
          meta.mainProgram = "pm-dashboard";
        };
      });

      apps = forAll (pkgs: {
        default = { type = "app"; program = "${self.packages.${pkgs.system}.default}/bin/pm"; };
        dashboard = { type = "app"; program = "${self.packages.${pkgs.system}.dashboard}/bin/pm-dashboard"; };
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.python3 pkgs.git ];
          shellHook = "git config core.hooksPath .githooks";
        };
      });
    };
}
