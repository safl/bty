package finf

import (
	"path/filepath"
	"time"
	"log"
	"os"
)

type Finf struct {
	Flags		uint8		`json:"flags"`
	Name		string		`json:"name"`
	Size		int64		`json:"size"`
	Mode		os.FileMode	`json:"mode"`
	ModTime		time.Time	`json:"mod_time"`

	Checksum	string		`json:"checksum"`
	Content		string		`json:"content"`
}

const (
	FINF_CHECKSUM uint8 = 1 << iota
	FINF_CONTENT
)

func FinfStat(fpath string, flags uint8) (Finf, error) {

	finf := Finf{Flags: flags};

	info, err := os.Stat(fpath)
	if err != nil {
		log.Printf("err: %v", err)
		return finf, err
	}

	finf.Name = info.Name()
	finf.Size = info.Size()
	finf.Mode = info.Mode()
	finf.ModTime = info.ModTime()

	if (flags & FINF_CHECKSUM != 0) {
		// TODO: implement checksum calculation
	}

	if (flags & FINF_CONTENT != 0) {
		// TODO: implement content load
	}

	return finf, nil
}

func FinfLoad(dpath string, glob string, flags uint8) []Finf {

	finfs := []Finf{}

	var fpaths, err = filepath.Glob(dpath + glob)
	if err != nil {
		log.Printf("filepath.Glob failed with err: %v", err)
		return finfs
	}

	for _, fpath := range fpaths {
		log.Printf("fpath: %s", fpath)
		finf, err := FinfStat(fpath, flags)
		if err != nil {
			log.Printf("skipping fpath: %s due to err", fpath)
			continue
		}

		finfs = append(finfs, finf)
	}

	return finfs
}

