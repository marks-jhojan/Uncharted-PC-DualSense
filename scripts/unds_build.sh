#!/usr/bin/env bash
# UNDS — build del port uncharted_pc <tag> + parches UNDS (patches/<tag>/ del repo UNDS),
# cada versión en su propio checkout (../uncharted_pc-0.3.9 junto al repo, ...): las anteriores quedan intactas
# para comparar o volver atrás.
# Ejecutar en la shell "MSYS2 CLANG64", con el launcher y el juego cerrados:
#   bash scripts/unds_build.sh [--deps]            # v0.3.9 (por defecto)
# Después, para el zip de release: bash scripts/unds_release.sh
# Variables (con sus valores por defecto):
#   UNDS_TAG      v0.3.9                                    tag upstream de base
#   UNDS_SRC      ../uncharted_pc-<tag sin v> (junto al repo) checkout de trabajo (se clona si no existe)
#   UNDS_BRANCH   unds-<tag sin v>                          rama con los parches UNDS
#   UNDS_PATCHES  <repo UNDS>/patches/<tag>                 serie de parches (git format-patch)
#   UNDS_OLD      ../uncharted_pc-0.3.8 (junto al repo)     build anterior (de su dist/ se copian partidas e .ini)
#   UNDS_RELEASE  (vacío)                                   release oficial de la que salen las DLL de DLSS
#                 (solo para jugar con dist/: el zip de unds_release.sh nunca lleva DLSS)
#                 (gpu/dlss_bridge no cambia entre v0.3.6 y v0.3.9: el bridge de 0.3.6 sirve)
#   WINPYTHON     python.exe de python.org para PyInstaller
set -euo pipefail

here=$(cd -- "$(dirname -- "$0")" && pwd)
TAG=${UNDS_TAG:-v0.3.9}
SRC=${UNDS_SRC:-$(dirname "$(dirname "$here")")/uncharted_pc-${TAG#v}}
BRANCH=${UNDS_BRANCH:-unds-${TAG#v}}
default_patches=$here/../patches/$TAG
[[ -d $default_patches ]] || default_patches=$here/$TAG          # copia suelta junto a las series viejas
PATCHES=${UNDS_PATCHES:-$default_patches}
OLD=${UNDS_OLD:-$(dirname "$(dirname "$here")")/uncharted_pc-0.3.8}
RELEASE=${UNDS_RELEASE:-/nonexistent}
log() { printf '\n\033[1;36m[UNDS] %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m[UNDS] ERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ ${MSYSTEM:-} == CLANG64 ]] || die "Abre la shell 'MSYS2 CLANG64' (MSYSTEM=${MSYSTEM:-vacío})."

if [[ ${1:-} == --deps ]]; then
    log "Instalando dependencias (lista de packaging/windows/README.md; igual en v0.3.6, v0.3.8 y v0.3.9)"
    pacman -S --needed --noconfirm git patch \
        mingw-w64-clang-x86_64-{toolchain,cmake,ninja,pkgconf,python} \
        mingw-w64-clang-x86_64-{vulkan-headers,vulkan-loader,vulkan-memory-allocator,glslang} \
        mingw-w64-clang-x86_64-{sdl3,fmt,boost,robin-map,xxhash,ffmpeg,zydis} \
        mingw-w64-clang-x86_64-{spirv-tools,spirv-headers,spirv-cross} \
        mingw-w64-clang-x86_64-{protobuf,openssl,zlib,miniupnpc,nlohmann-json}
fi
for tool in git clang cmake ninja pkg-config python sha256sum; do
    command -v "$tool" >/dev/null || die "Falta $tool: ejecuta 'bash unds_build.sh --deps'."
done
log "SDL3 de MSYS2: $(pkg-config --modversion sdl3)"

shopt -s nullglob
series=("$PATCHES"/0*.patch)
shopt -u nullglob
(( ${#series[@]} >= 3 )) || die "Esperaba al menos 3 parches en $PATCHES (hay ${#series[@]})."
(( ${#series[@]} >= 4 )) || log "AVISO: $PATCHES no trae el 0004 (canal fx, Día 8): el companion necesitará --backend hid."

if [[ ! -d $SRC/.git ]]; then
    log "Clonando $TAG con submódulos en $SRC (reutiliza los objetos de $OLD si existe)"
    mkdir -p "$(dirname "$SRC")"
    ref=(); [[ -d $OLD/.git ]] && ref=(--reference-if-able "$OLD" --dissociate)
    git clone "${ref[@]}" --branch "$TAG" https://github.com/Supermedo/uncharted_pc.git "$SRC"
fi
cd "$SRC"
git fetch -q --tags origin || log "AVISO: sin red para 'git fetch'; sigo con lo que hay"
git rev-parse -q --verify "$TAG^{commit}" >/dev/null || die "No existe el tag $TAG en $SRC."
if ! git rev-parse --verify -q "$BRANCH" >/dev/null; then
    [[ -z $(git status --porcelain --untracked-files=no --ignore-submodules=all) ]] || die "Árbol con cambios en $SRC: guárdalos o descártalos."
    git checkout -q -b "$BRANCH" "$TAG"
else
    git checkout -q "$BRANCH"
fi
git submodule update --init --recursive

# Solo los parches que faltan en la rama (por asunto): una rama con 0001-0003 recibe solo el 0004.
applied=$(git log --format=%s "$TAG..HEAD")
missing=()
tmp=$(mktemp -d)
for p in "${series[@]}"; do
    subject=$(git mailinfo "$tmp/msg" "$tmp/patch" < "$p" | sed -n 's/^Subject: //p')
    [[ -n $subject ]] || die "No leo el asunto de $p."
    grep -qxF -- "$subject" <<<"$applied" || missing+=("$p")
done
rm -rf "$tmp"
if (( ${#missing[@]} == 0 )); then
    log "Parches UNDS ya aplicados en $BRANCH ($(git log --oneline -1 | cut -c1-80))"
else
    [[ -z $(git status --porcelain --untracked-files=no --ignore-submodules=all) ]] || die "Árbol con cambios en $SRC: guárdalos o descártalos antes de aplicar ${missing[*]##*/}."
    log "Aplicando ${#missing[@]} parche(s) UNDS sobre $BRANCH: ${missing[*]##*/}"
    git -c user.name=unds -c user.email=unds@localhost am --3way "${missing[@]}" \
        || { git am --abort || true; die "git am falló (¿árbol modificado?)."; }
fi

log "Tests de prepare.py (camino SELF y ELF plano, ya upstream desde v0.3.7)"
python -m unittest discover -s tests -p 'test_prepare.py' || die "Tests de prepare.py fallan."

mkdir -p out
if [[ -f $RELEASE/bin/bbport_dlss.dll && -f $RELEASE/bin/nvngx_dlss.dll ]]; then
    lic=$(ls "$RELEASE"/licenses/*NVIDIA*DLSS* 2>/dev/null | head -1 || true)
    if [[ -n $lic ]]; then
        log "Reutilizando DLSS de $RELEASE"
        cp "$RELEASE/bin/bbport_dlss.dll" "$RELEASE/bin/nvngx_dlss.dll" out/
        [[ -f $RELEASE/bin/nvngx_dlssg.dll ]] && cp "$RELEASE/bin/nvngx_dlssg.dll" out/
        cp "$lic" out/NVIDIA-DLSS-LICENSE.txt
    else
        log "AVISO: sin licencia NVIDIA en $RELEASE/licenses: el paquete saldrá sin DLSS"
    fi
else
    log "AVISO: no hay DLL de DLSS en $RELEASE/bin: el paquete saldrá sin DLSS (usa FSR/TAA para probar)"
fi

if [[ -z ${WINPYTHON:-} ]]; then
    candidates=()
    if [[ -n ${LOCALAPPDATA:-} ]]; then
        candidates+=("$(cygpath -u "$LOCALAPPDATA")"/Programs/Python/Python3*/python.exe)
    fi
    candidates+=(/c/Python3*/python.exe "/c/Program Files"/Python3*/python.exe)
    if command -v py.exe >/dev/null; then
        found=$(py.exe -3 -c "import sys; print(sys.executable)" 2>/dev/null | tr -d '\r' || true)
        [[ -n $found ]] && candidates+=("$(cygpath -u "$found")")
    fi
    for candidate in "${candidates[@]}"; do
        [[ -x $candidate && $candidate != *WindowsApps* ]] && WINPYTHON=$candidate
    done
fi
[[ -n ${WINPYTHON:-} ]] || die "No encuentro Python de python.org. En CMD: winget install Python.Python.3.12; luego reabre CLANG64 y repite."
export WINPYTHON
log "Python de Windows para el empaquetado: $WINPYTHON ($("$WINPYTHON" --version 2>&1 | tr -d '\r'))"

log "Compilando (build.sh) — el log completo queda en out/gpu-build.log"
time bash build.sh

log "Test del bus UNDS (unds-test: serializador, umbrales, estado como el de U1, canal fx)"
ninja -C out/gpu unds-test >/dev/null || die "unds-test no compila (mira: ninja -C out/gpu unds-test)."
out/gpu/unds-test.exe | tail -1 || die "unds-test falla."

if [[ ! -f launcher/uncharted.png ]]; then
    for from in "$RELEASE/launcher/uncharted.png" "$OLD/launcher/uncharted.png"; do
        if [[ -f $from ]]; then log "launcher/uncharted.png falta en el repo: copiado de $from"; cp "$from" launcher/; break; fi
    done
fi
if [[ ! -f launcher/uncharted.png ]]; then
    log "launcher/uncharted.png falta en el repo: generándolo con make_uncharted_icon.py"
    [[ -x out/pyenv/Scripts/python.exe ]] || "$WINPYTHON" -m venv out/pyenv
    out/pyenv/Scripts/python.exe -m pip install -q --disable-pip-version-check pillow
    out/pyenv/Scripts/python.exe packaging/windows/make_uncharted_icon.py \
        "$(cygpath -w "$PWD/out/uncharted-regen.ico")" "$(cygpath -w "$PWD/launcher/uncharted.png")"
fi

log "Empaquetando (packaging/windows/package.sh)"
if ! bash packaging/windows/package.sh; then
    stage=out/stage/uncharted-windows
    [[ -f $stage/bin/bb-probe.exe && -f $stage/Uncharted.exe ]] || die "package.sh falló antes de montar $stage."
    log "AVISO: package.sh terminó con error tras montar el paquete; copiando $stage a dist/ (se conservan user/ y los .ini)"
    mkdir -p dist/uncharted-windows
    find dist/uncharted-windows -mindepth 1 -maxdepth 1 ! -name user ! -name out ! -name art ! -name bbport.ini \
        ! -name input.ini ! -name last_run.log -exec rm -rf {} +
    cp -r "$stage/." dist/uncharted-windows/
fi

built=$(sha256sum out/stage/uncharted-windows/bin/bb-probe.exe | cut -d' ' -f1)
played=$(sha256sum dist/uncharted-windows/bin/bb-probe.exe 2>/dev/null | cut -d' ' -f1 || true)
if [[ -z $built || $built != "$played" ]]; then
    die "dist/uncharted-windows NO se actualizó (¿launcher o juego abiertos?). Ciérralos y repite: bash unds_build.sh"
fi
log "dist/ actualizado: bin/bb-probe.exe = el recién compilado"

# Primera vez: partidas y ajustes del build anterior ($OLD) (solo lo que aún no exista; la caché de shaders
# no se copia, se rehace).
old_dist=$OLD/dist/uncharted-windows
new_dist=dist/uncharted-windows
if [[ -d $old_dist ]]; then
    for f in bbport.ini input.ini; do
        if [[ -f $old_dist/$f && ! -f $new_dist/$f ]]; then cp "$old_dist/$f" "$new_dist/"; log "Copiado $f de $OLD"; fi
    done
    if [[ -d $old_dist/user/savedata && ! -d $new_dist/user/savedata ]]; then
        mkdir -p "$new_dist/user" && cp -r "$old_dist/user/savedata" "$new_dist/user/" && log "Copiadas las partidas (user/savedata) de $OLD"
    fi
fi
log "Hecho. Build $TAG + UNDS en: $(cygpath -w "$SRC/dist/uncharted-windows")"
git log --oneline -4
