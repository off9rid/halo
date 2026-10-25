// One-shot window/app actions for HALO. Usage: osascript -l JavaScript target.js <halo pid> quit|minimize
// Acts on the frontmost normal window that isn't HALO's own. Prints "<verb> <app>", "protected <app>" or "none".
ObjC.import('CoreGraphics');

// Never quit these: Terminal runs HALO (and Cortex), Finder can't really quit.
var PROTECTED = ['Terminal', 'iTerm2', 'Warp', 'Finder', 'loginwindow'];

function run(argv) {
  var selfPid = parseInt(argv[0], 10), verb = argv[1];
  var list = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo(1 | 16, 0))) || [];
  var target = list.find(function (w) {
    var b = w.kCGWindowBounds;
    return w.kCGWindowLayer === 0 && w.kCGWindowOwnerPID !== selfPid && b.Width > 80 && b.Height > 80;
  });
  if (!target) return 'none';
  var name = target.kCGWindowOwnerName;
  var proc = Application('System Events').processes.whose({ unixId: target.kCGWindowOwnerPID })[0];

  if (verb === 'quit') {
    if (PROTECTED.indexOf(name) !== -1) return 'protected ' + name;
    Application(proc.bundleIdentifier()).quit();   // polite quit: apps still ask to save
    return 'quit ' + name;
  }
  if (verb === 'minimize') {
    var b = target.kCGWindowBounds, wins = proc.windows();
    for (var i = 0; i < wins.length; i++) {
      var p = wins[i].position();
      if (Math.abs(p[0] - b.X) < 3 && Math.abs(p[1] - b.Y) < 3) {
        wins[i].attributes.byName('AXMinimized').value = true;
        return 'minimize ' + name;
      }
    }
    return 'none';
  }
  return 'error unknown verb ' + verb;
}
