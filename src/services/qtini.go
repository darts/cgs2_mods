package main

import (
	"errors"
	"os"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// readQtINI reads string values of a QSettings INI file as Qt 5.14 decodes them
// (QSettingsPrivate::iniUnescapedStringList). The native app and Qt.labs.settings
// write these files; values such as MQTT passwords may contain escapes.
func readQtINI(p string) (map[string]map[string]string, error) {
	b, e := os.ReadFile(p)
	if e != nil {
		return nil, e
	}
	if len(b) > 256*1024 {
		return nil, errors.New("settings file too large")
	}
	sections := map[string]map[string]string{}
	section := "General"
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimRight(line, "\r")
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || trimmed[0] == ';' || trimmed[0] == '#' {
			continue
		}
		if strings.HasPrefix(trimmed, "[") && strings.HasSuffix(trimmed, "]") {
			section = trimmed[1 : len(trimmed)-1]
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		if sections[section] == nil {
			sections[section] = map[string]string{}
		}
		sections[section][strings.TrimSpace(key)] = qtValue(value)
	}
	return sections, nil
}

// qtValue decodes one raw INI value. A comma outside quotes makes Qt read a
// string list; this returns "" for lists and variants, which are never strings.
func qtValue(raw string) string {
	var units []uint16
	plain := func(s string) {
		// Without an INI codec Qt escapes every non-ASCII character, so raw
		// bytes above 0x7f only appear in files written with a UTF-8 codec.
		if !utf8.ValidString(s) {
			for i := 0; i < len(s); i++ {
				units = append(units, uint16(s[i]))
			}
			return
		}
		units = append(units, utf16.Encode([]rune(s))...)
	}
	escapes := map[byte]uint16{'a': '\a', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t', 'v': '\v', '"': '"', '?': '?', '\'': '\'', '\\': '\\'}
	hex := func(c byte) (int, bool) {
		switch {
		case c >= '0' && c <= '9':
			return int(c - '0'), true
		case c >= 'a' && c <= 'f':
			return int(c-'a') + 10, true
		case c >= 'A' && c <= 'F':
			return int(c-'A') + 10, true
		}
		return 0, false
	}
	i := 0
	skipSpaces := func() {
		for i < len(raw) && (raw[i] == ' ' || raw[i] == '\t') {
			i++
		}
	}
	skipSpaces()
	quoted, inQuotes := false, false
	// Trailing spaces are dropped only from unquoted text after the last escape.
	chop := 0
	for i < len(raw) {
		switch c := raw[i]; c {
		case '\\':
			i++
			if i >= len(raw) {
				break
			}
			c = raw[i]
			i++
			if v, ok := escapes[c]; ok {
				units = append(units, v)
			} else if c == 'x' {
				v, n := 0, 0
				for i < len(raw) {
					d, ok := hex(raw[i])
					if !ok {
						break
					}
					v, n = v<<4+d, n+1
					i++
				}
				if n > 0 {
					units = append(units, uint16(v))
				}
			} else if c >= '0' && c <= '7' {
				v := int(c - '0')
				for i < len(raw) && raw[i] >= '0' && raw[i] <= '7' {
					v = v<<3 + int(raw[i]-'0')
					i++
				}
				units = append(units, uint16(v))
			}
			chop = len(units)
		case '"':
			i++
			quoted = true
			inQuotes = !inQuotes
			if !inQuotes {
				skipSpaces()
			}
		case ',':
			if !inQuotes {
				return ""
			}
			fallthrough
		default:
			j := i + 1
			for j < len(raw) && raw[j] != '\\' && raw[j] != '"' && raw[j] != ',' {
				j++
			}
			plain(raw[i:j])
			i = j
		}
	}
	if !quoted {
		for len(units) > chop && (units[len(units)-1] == ' ' || units[len(units)-1] == '\t') {
			units = units[:len(units)-1]
		}
	}
	s := string(utf16.Decode(units))
	// QSettings::stringToVariant: "@@" escapes a leading "@"; the typed forms
	// below are not plain strings.
	if strings.HasPrefix(s, "@@") {
		return s[1:]
	}
	if strings.HasSuffix(s, ")") {
		for _, typed := range []string{"@ByteArray(", "@String(", "@Variant(", "@DateTime(", "@Rect(", "@Size(", "@Point(", "@Invalid("} {
			if strings.HasPrefix(s, typed) {
				return ""
			}
		}
	}
	return s
}
