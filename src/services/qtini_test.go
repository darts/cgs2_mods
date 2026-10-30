package main

import (
	"os"
	"path/filepath"
	"testing"
)

// Written by Qt.labs.settings on the monitor (Qt 5.14.2); want holds what Qt
// itself read back from the same file.
const qtVectors = `[third]
at=@@start
atat=@@@twice
backslash=c:\\path\\x41
cjk=\x65e5\x672c\x8a9e
comma="a,b"
emoji=key \xd83d\xde00 end
empty=
equals="a=b"
hash=#tag
hexnext=\xe9\x31\x61\x46
leading=" space"
mixed=" \"q\", \\ \xe9;=@ "
percent=100%
port=1883
quote=say \"hi\"
semicolon="a;b"
simple=broker-user
tls=1
trailing="space "
typed=@@Variant(x)
umlaut=p\xe4ssw\xf6rd
`

func TestReadQtINIMatchesQtOnTheMonitor(t *testing.T) {
	p := filepath.Join(t.TempDir(), "vectors.ini")
	if err := os.WriteFile(p, []byte(qtVectors), 0600); err != nil {
		t.Fatal(err)
	}
	sections, err := readQtINI(p)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{
		"at":        "@start",
		"atat":      "@@twice",
		"backslash": "c:\\path\\x41",
		"cjk":       "日本語",
		"comma":     "a,b",
		"emoji":     "key 😀 end",
		"empty":     "",
		"equals":    "a=b",
		"hash":      "#tag",
		"hexnext":   "é1aF",
		"leading":   " space",
		"mixed":     " \"q\", \\ é;=@ ",
		"percent":   "100%",
		"port":      "1883",
		"quote":     "say \"hi\"",
		"semicolon": "a;b",
		"simple":    "broker-user",
		"tls":       "1",
		"trailing":  "space ",
		"typed":     "@Variant(x)",
		"umlaut":    "pässwörd",
	}
	for key, value := range want {
		got, ok := sections["third"][key]
		if !ok || got != value {
			t.Errorf("%s: got %q, want %q", key, got, value)
		}
	}
}

func TestQtValueRejectsListsAndTypedValues(t *testing.T) {
	for raw, want := range map[string]string{
		"a, b":             "",
		"@Variant(\\0\\0)": "",
		"@ByteArray(abc)":  "",
		"@plain":           "@plain",
		"\\x":              "",
		"unquoted  ":       "unquoted",
		"\\t ":             "\t",
	} {
		if got := qtValue(raw); got != want {
			t.Errorf("%q: got %q, want %q", raw, got, want)
		}
	}
}
