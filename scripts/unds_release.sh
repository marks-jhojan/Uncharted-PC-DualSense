#!/usr/bin/env bash
# UNDS — paquete de release para Windows: se extrae ENCIMA de la release oficial del port (misma versión).
#
# Ejecutar en la shell "MSYS2 CLANG64" después de compilar el port con los parches UNDS:
#   bash scripts/unds_build.sh          # port v0.3.9 + patches/v0.3.9 (checkout en ../uncharted_pc-0.3.9, junto a este repo)
#   bash scripts/unds_release.sh        # UNDS.exe (PyInstaller) + zip en build/
#
# El zip lleva (carpeta raíz uncharted-windows/, como el zip oficial):
#   run.py                  el del port con el parche 0007 (Mesh raycaster copies automático en U1 y U2);
#                           Uncharted.exe lo ejecuta desde disco (runpy), no hace falta otro Uncharted.exe
#   bin/                    bb-probe.exe con los parches UNDS y las DLL de CLANG64 con las que se compiló,
#                           SIN las de DLSS ni XeSS (licencias de NVIDIA e Intel: se quedan las de la oficial)
#   licenses/               las del paquete (menos NVIDIA, bridge de DLSS e Intel XeSS)
#   UNDS/                   UNDS.exe (companion + lanzador + Studio, sin Python), "UNDS Studio.cmd", sus .ini,
#                           LEEME/README, LICENSE y
#                           los parches (fuente del bb-probe.exe junto con el tag upstream)
# Variables: UNDS_TAG (v0.3.9), UNDS_SRC (../uncharted_pc-<tag sin v>, junto a este repo), WINPYTHON (python.exe de python.org).
set -euo pipefail

here=$(cd -- "$(dirname -- "$0")" && pwd)
repo=$(dirname "$here")
TAG=${UNDS_TAG:-v0.3.9}
SRC=${UNDS_SRC:-$(dirname "$repo")/uncharted_pc-${TAG#v}}
log() { printf '\n\033[1;36m[UNDS] %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m[UNDS] ERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ ${MSYSTEM:-} == CLANG64 ]] || die "Abre la shell 'MSYS2 CLANG64' (MSYSTEM=${MSYSTEM:-vacío})."
VERSION=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$repo/companion/unds_launcher.py")
[[ -n $VERSION ]] || die "No leo VERSION de companion/unds_launcher.py."
grep -q "^PORT_TAG = \"$TAG\"$" "$repo/companion/unds_launcher.py" || die "PORT_TAG de unds_launcher.py no es $TAG."
stage=$SRC/out/stage/uncharted-windows
[[ -f $stage/bin/bb-probe.exe && -f $SRC/run.py ]] || die "No hay paquete del port en $stage: bash scripts/unds_build.sh"

log "Comprobando que $SRC tiene la serie patches/$TAG y que el paquete es de ese árbol"
applied=$(git -C "$SRC" log --format=%s "$TAG..HEAD")
tmp=$(mktemp -d)
for p in "$repo/patches/$TAG"/0*.patch; do
    subject=$(git mailinfo "$tmp/msg" "$tmp/patch" < "$p" | sed -n 's/^Subject: //p')
    grep -qxF -- "$subject" <<<"$applied" || die "Falta en $SRC: $subject (bash scripts/unds_build.sh)"
done
rm -rf "$tmp"
[[ -z $(git -C "$SRC" status --porcelain --untracked-files=no --ignore-submodules=all) ]] \
    || die "$SRC tiene cambios sin commit: el binario no correspondería a los parches publicados."
for name in UNDS_EVENTS UNDS_FX UNDS_STATE; do
    grep -aq "$name" "$stage/bin/bb-probe.exe" || die "$stage/bin/bb-probe.exe no contiene $name: recompila."
done
[[ $(stat -c %Y "$stage/bin/bb-probe.exe") -ge $(git -C "$SRC" log -1 --format=%ct) ]] \
    || die "El paquete es más viejo que el último commit de $SRC: bash scripts/unds_build.sh"
git -C "$SRC" diff --quiet HEAD -- run.py || die "run.py de $SRC no es el del commit."

if [[ -z ${WINPYTHON:-} ]]; then
    candidates=()
    [[ -n ${LOCALAPPDATA:-} ]] && candidates+=("$(cygpath -u "$LOCALAPPDATA")"/Programs/Python/Python3*/python.exe)
    candidates+=(/c/Python3*/python.exe "/c/Program Files"/Python3*/python.exe)
    for candidate in "${candidates[@]}"; do
        [[ -x $candidate && $candidate != *WindowsApps* ]] && WINPYTHON=$candidate
    done
fi
[[ -n ${WINPYTHON:-} ]] || die "No encuentro Python de python.org (WINPYTHON=ruta\\python.exe)."
export USERPROFILE=${USERPROFILE:-$(cygpath -w "/c/Users/$(id -un)")}
log "Python para PyInstaller: $WINPYTHON ($("$WINPYTHON" --version 2>&1 | tr -d '\r'))"

build=$repo/build
py=$build/pyenv/Scripts/python.exe
[[ -x $py ]] || "$WINPYTHON" -m venv "$build/pyenv"
"$py" -m pip install -q --disable-pip-version-check -r "$(cygpath -w "$repo/companion/requirements-release.txt")"

log "Tests del companion (lanzador y aviso por lightbar)"
"$py" -m unittest discover -s "$(cygpath -w "$repo/tests")" -p 'test_*.py' || die "Tests del companion fallan."

log "UNDS.exe (PyInstaller, carpeta, consola)"
hidden=()
for module in unds unds_triggers unds_haptics unds_listen unds_port unds_hid unds_dsx unds_studio hid winsound; do
    hidden+=(--hidden-import "$module")
done
"$py" -m PyInstaller --noconfirm --clean --log-level WARN --console --onedir --name UNDS \
    --distpath "$(cygpath -w "$build/pyi-dist")" --workpath "$(cygpath -w "$build/pyi-work")" \
    --specpath "$(cygpath -w "$build/pyi-work")" --paths "$(cygpath -w "$repo/companion")" \
    --add-data "$(cygpath -w "$repo/companion/studio.html");." \
    "${hidden[@]}" "$(cygpath -w "$repo/companion/unds_launcher.py")"
[[ -f $build/pyi-dist/UNDS/UNDS.exe ]] || die "PyInstaller no dejó build/pyi-dist/UNDS/UNDS.exe."
log "Prueba de humo: UNDS.exe --selftest"
"$build/pyi-dist/UNDS/UNDS.exe" --selftest || die "UNDS.exe --selftest falla (bibliotecas sin empaquetar)."

log "Montando el paquete"
out=$build/stage/uncharted-windows
rm -rf -- "$build/stage"
mkdir -p "$out/bin" "$out/licenses" "$out/UNDS/source"
cp "$SRC/run.py" "$out/"
for f in "$stage"/bin/*; do
    case ${f##*/} in bbport_dlss.dll|nvngx_*.dll|libxess*.dll) continue ;; esac
    cp "$f" "$out/bin/"
done
for f in "$stage"/licenses/*; do
    case ${f##*/} in NVIDIA*|bbport_dlss*|Intel-XeSS*) continue ;; esac
    cp "$f" "$out/licenses/"
done
cp -r "$build/pyi-dist/UNDS/." "$out/UNDS/"
cp "$repo"/companion/unds_triggers.ini "$repo"/companion/unds_haptics.ini "$repo"/companion/unds_launcher.ini "$out/UNDS/"
cp "$repo/packaging/LEEME.txt" "$out/UNDS/LEEME.txt"
cp "$repo/packaging/UNDS Studio.cmd" "$out/UNDS/UNDS Studio.cmd"
cp "$repo/packaging/README.txt" "$out/UNDS/README.txt"
cp "$repo/LICENSE" "$out/UNDS/LICENSE.txt"
cp "$repo/patches/$TAG"/0*.patch "$out/UNDS/source/"
printf 'UNDS %s para uncharted_pc %s (https://github.com/Supermedo/uncharted_pc/tree/%s)\nbb-probe.exe = %s + estos parches (git am)\nport: %s\n' \
    "$VERSION" "$TAG" "$TAG" "$TAG" "$(git -C "$SRC" rev-parse HEAD)" > "$out/UNDS/source/SOURCE.txt"

name=Uncharted-PC-DualSense-$VERSION-uncharted_pc-${TAG#v}-windows
rm -f "$build/$name.zip"
(cd "$build/stage" && "$py" -c "
import pathlib, zipfile
with zipfile.ZipFile('../$name.zip', 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for p in sorted(pathlib.Path('uncharted-windows').rglob('*')): z.write(p)")
(cd "$build" && sha256sum "$name.zip" > "$name.zip.sha256")
log "Hecho: $(cygpath -w "$build/$name.zip") ($(du -h "$build/$name.zip" | cut -f1))"
cat "$build/$name.zip.sha256"
