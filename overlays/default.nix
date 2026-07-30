final: _prev:
let
  inherit (final) lib callPackage;

  plugins = builtins.fromJSON (builtins.readFile ../data/plugins.json);
  themes = builtins.fromJSON (builtins.readFile ../data/themes.json);

  mkPlugin = callPackage ./mk-plugin.nix { };
  mkTheme = callPackage ./mk-theme.nix { };
in
{
  obsidianPlugins = lib.mapAttrs (id: meta: mkPlugin (meta // { inherit id; })) plugins;
  obsidianThemes = lib.mapAttrs (attrName: meta: mkTheme (meta // { inherit attrName; })) themes;
}
