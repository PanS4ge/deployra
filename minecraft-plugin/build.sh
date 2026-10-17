#!/usr/bin/env sh
# Buduje DeployraBridge.jar bez Mavena: javac --release 8 względem jarów API w libs/ (paper-api i zależności,
# pobierz je z repo.papermc.io albo zbuduj przez `mvn package`). Wynik trafia do pobierania w panelu.
set -eu
cd "$(dirname "$0")"
OUT=../frontend/deployra_panel/static/downloads/DeployraBridge.jar
SEP=":"; case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) SEP=";";; esac
rm -rf build && mkdir -p build/classes
javac --release 8 -Xlint:-options -encoding UTF-8 -cp "libs/*" -d build/classes src/main/java/io/deployra/minecraft/*.java
cp src/main/resources/plugin.yml src/main/resources/config.yml build/classes/
jar --create --file "$OUT" -C build/classes .
rm -rf build
echo "zbudowano $OUT"
