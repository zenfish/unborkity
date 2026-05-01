# Builds a deliberately-borked binary for unborkity tests.
#
# Layout after `make`:
#   build/
#     lib/libgreet.dylib           ← real, working dylib (install_name = @rpath/libgreet.dylib)
#     bin/mygreet                  ← linked against @rpath/libgreet.dylib but with NO LC_RPATH
#                                    (mimics the typical Homebrew bork)
#     stash/libgreet.dylib         ← second copy, used as a "found by mdfind/walk" candidate
#
# The bin/mygreet binary will fail at runtime with the classic
# "dyld: Library not loaded ... Reason: image not found" until repaired.

CC      ?= clang
CFLAGS  ?= -O0 -g -Wall

BUILD   := build
LIBDIR  := $(BUILD)/lib
BINDIR  := $(BUILD)/bin
STASH   := $(BUILD)/stash

DYLIB   := $(LIBDIR)/libgreet.dylib
STASHED := $(STASH)/libgreet.dylib
BIN     := $(BINDIR)/mygreet

.PHONY: all clean
all: $(BIN) $(STASHED)

$(LIBDIR) $(BINDIR) $(STASH):
	mkdir -p $@

$(DYLIB): libgreet.c | $(LIBDIR)
	$(CC) $(CFLAGS) -dynamiclib \
	    -install_name @rpath/libgreet.dylib \
	    -o $@ $<
	codesign -f -s - $@

# Link mygreet against @rpath/libgreet.dylib WITHOUT adding an LC_RPATH.
# Result: otool -L shows @rpath/libgreet.dylib; running the binary fails
# until an rpath is added (or the ref is rewritten to an absolute path).
$(BIN): mygreet.c $(DYLIB) | $(BINDIR)
	$(CC) $(CFLAGS) -o $@ mygreet.c $(DYLIB)
	# strip any LC_RPATH that the toolchain auto-added
	@for rp in $$(otool -l $@ | awk '/cmd LC_RPATH/{f=1;next} f && /path /{print $$2; f=0}'); do \
	    install_name_tool -delete_rpath $$rp $@ || true; \
	done
	codesign -f -s - $@

# Stash a second copy in a directory the test can point unborkity at.
$(STASHED): $(DYLIB) | $(STASH)
	cp $< $@
	codesign -f -s - $@

clean:
	rm -rf $(BUILD)
