//go:build linux

package main

import (
	"context"
	"fmt"
	"os/exec"
	"syscall"
	"time"
)

func setClock(t time.Time) error {
	tv := syscall.NsecToTimeval(t.UnixNano())
	if err := syscall.Settimeofday(&tv); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if output, err := exec.CommandContext(ctx, "hwclock", "-w", "-u").CombinedOutput(); err != nil {
		return fmt.Errorf("system time updated, but saving the hardware clock failed: %v (%s)", err, output)
	}
	return nil
}
