package main

import (
	"os"
	"sync"
)

const serviceLogLimit = 1024 * 1024

// Only the supervisor owns this writer. Child output is copied through it by
// os/exec, so rotation does not leave a worker writing to an unlinked file.
type serviceLog struct {
	mu   sync.Mutex
	name string
}

func (l *serviceLog) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	n := len(p)
	if len(p) > serviceLogLimit {
		p = p[len(p)-serviceLogLimit:]
	}
	info, err := os.Stat(l.name)
	if err != nil && !os.IsNotExist(err) {
		return 0, err
	}
	if info != nil && info.Size()+int64(len(p)) > serviceLogLimit {
		if err = os.Remove(l.name + ".old"); err != nil && !os.IsNotExist(err) {
			return 0, err
		}
		// Bound a large log left by an older version before retaining it.
		if info.Size() > serviceLogLimit {
			if err = os.Truncate(l.name, serviceLogLimit); err != nil {
				return 0, err
			}
		}
		if err = os.Rename(l.name, l.name+".old"); err != nil {
			return 0, err
		}
	}
	f, err := os.OpenFile(l.name, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		return 0, err
	}
	_, err = f.Write(p)
	closeErr := f.Close()
	if err != nil {
		return 0, err
	}
	return n, closeErr
}
