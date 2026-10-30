//go:build !linux

package main

import (
	"errors"
	"time"
)

func setClock(time.Time) error {
	return errors.New("clock changes are supported only on the CGS2 Linux target")
}
