{
  lib,
  fetchurl,
  runCommandLocal,
}:
{
  attrName,
  name ? attrName,
  repo,
  rev,
  files,
  modes ? [ ],
  ...
}:
let
  assets = lib.mapAttrsToList (fname: hash: {
    inherit fname;
    drv = fetchurl {
      url = "https://raw.githubusercontent.com/${repo}/${rev}/${fname}";
      inherit hash;
      name = "obsidian-theme-${attrName}-${fname}";
    };
  }) files;
in
runCommandLocal "obsidian-theme-${attrName}"
  {
    passthru = {
      manifestId = name;
      inherit repo rev modes;
    };
    meta = {
      description = "Obsidian community theme: ${name}";
      homepage = "https://github.com/${repo}";
    };
  }
  ''
    mkdir -p "$out"
    ${lib.concatMapStringsSep "\n" ({ fname, drv }: ''cp "${drv}" "$out/${fname}"'') assets}
  ''
