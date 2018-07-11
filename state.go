package state

import (
//	"path/filepath"
	"time"
	"os"
)

type Osi struct {
	Fname		string		`json:"fname"`
	Fsize		int64		`json:"fsize"`
	Fmode		os.FileMode	`json:"fmode"`
	Ftmod		time.Time	`json:"ftmod"`

	Checksum	string		`json:"checksum"`
}

type Bzi struct {
	Fname		string		`json:"fname"`
	Fsize		int64		`json:"fsize"`
	Fmode		os.FileMode	`json:"fmode"`
	Ftmod		time.Time	`json:"ftmod"`

	Checksum	string		`json:"checksum"`
}

type Pconfig struct {
	Fname		string		`json:"fname"`
	Fsize		int64		`json:"fsize"`
	Fmode		os.FileMode	`json:"fmode"`
	Ftmod		time.Time	`json:"ftmod"`

	Checksum	string		`json:"checksum"`
	Content		string		`json:"content"`
}

type Ptemplate struct {
	Fname		string		`json:"fname"`
	Fsize		int64		`json:"fsize"`
	Fmode		os.FileMode	`json:"fmode"`
	Ftmod		time.Time	`json:"ftmod"`

	Checksum	string		`json:"checksum"`
	Content		string		`json:"content"`

	Plabels		[]string	`json:"plabels"`
}

type Machine struct {
	hwa		string		`json:"hwa"`
	Hostname	string		`json:"hostname"`
	managed		bool		`json:"managed"`
	osi		Osi		`json:"osi"`
	bzi		Bzi		`json:"bzi"`
	plabel		string		`json:"plabel"`
	ptemplate	Ptemplate	`json:"ptemlate"`
}

type State struct {
	Config		Config		`json:"config"`

	Osis		[]Osi		`json:"osis"`
	Bzis		[]Bzi		`json:"bzis"`
	Pconfigs	[]Pconfig	`json:"pconfigs"`
	Ptemplates	[]Ptemplate	`json:"ptemplates"`
	machines	[]Machine	`json:"machines"`
}

// Load Operating System Disk Images
// TODO: fix checksum
func osis_load(cfg Config, osis *[]Osi, flags int) {

	var fnames, err = filepath.Glob(cfg.Locs.Osis + cfg.Patterns.OsiExt)
	if err != nil {
		log.Printf("err: %v", err)
		return
	}

	for _, fname := range fnames {
		info, err := os.Stat(fname)
		if err != nil {
			log.Printf("err: %v", err)
			continue
		}

		*osis = append(*osis, Osi{
			Fname: fname,
			Fsize: info.Size(),
			Fmode: info.Mode(),
			Ftmod: info.ModTime(),

			Checksum: "",
		})
	}
}

// Load Operating System Disk Images
// TODO: fix checksum
func bzis_load(cfg Config, bzis *[]Bzi, flags int) {

	var fnames, err = filepath.Glob(cfg.Locs.Bzis + cfg.Patterns.BziExt)
	if err != nil {
		log.Printf("err: %v", err)
		return
	}

	for _, fname := range fnames {
		info, err := os.Stat(fname)
		if err != nil {
			log.Printf("err: %v", err)
			continue
		}

		*bzis = append(*bzis, Bzi{
			Fname: fname,
			Fsize: info.Size(),
			Fmode: info.Mode(),
			Ftmod: info.ModTime(),

			Checksum: "",
		})
	}
}

