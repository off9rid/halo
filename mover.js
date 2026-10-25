// Window mover for HALO. Long-running; reads commands on stdin:
//   grab            pick the frontmost normal window (not ours) and remember it
//   move DX DY      move it by DX, DY (fractions of screen width/height) from where it was grabbed
//   scale K         resize it by factor K around its centre (several in one read multiply together)
//   release         forget it
// Replies on stdout: "grabbed <app>", "none", or "error <msg>".
ObjC.import('CoreGraphics');
ObjC.import('AppKit');

function run(argv) {
  var selfPid = parseInt(argv[0], 10);
  var se = Application('System Events');
  var screen = $.NSScreen.mainScreen.frame.size;
  var stdin = $.NSFileHandle.fileHandleWithStandardInput;
  var buf = '';
  var win = null, start = null;

  function say(s) {
    $.NSFileHandle.fileHandleWithStandardOutput.writeData(
      $(s + '\n').dataUsingEncoding($.NSUTF8StringEncoding));
  }

  function grab() {
    win = null;
    var list = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo(1 | 16, 0))) || [];
    var target = list.find(function (w) {
      var b = w.kCGWindowBounds;
      return w.kCGWindowLayer === 0 && w.kCGWindowOwnerPID !== selfPid && b.Width > 80 && b.Height > 80;
    });
    if (!target) return say('none');
    var b = target.kCGWindowBounds;
    var wins = se.processes.whose({ unixId: target.kCGWindowOwnerPID })[0].windows();
    for (var i = 0; i < wins.length; i++) {
      var p = wins[i].position();
      if (Math.abs(p[0] - b.X) < 3 && Math.abs(p[1] - b.Y) < 3) {
        win = wins[i];
        start = p;
        return say('grabbed ' + target.kCGWindowOwnerName);
      }
    }
    say('none');
  }

  while (true) {
    var data = stdin.availableData;
    if (Number(data.length) === 0) return;  // Python quit (length arrives as a string)
    buf += $.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding).js;
    var lines = buf.split('\n');
    buf = lines.pop();

    // Only the newest move matters; grabs and releases run in order; scales multiply.
    var move = null, sc = 1;
    lines.forEach(function (line) {
      var parts = line.trim().split(' ');
      try {
        if (parts[0] === 'grab') { move = null; grab(); }
        else if (parts[0] === 'release') { move = null; win = null; }
        else if (parts[0] === 'move') move = parts;
        else if (parts[0] === 'scale') sc *= parseFloat(parts[1]) || 1;
      } catch (e) { win = null; say('error ' + e.message); }
    });
    if (sc !== 1 && win) {
      try {
        var p = win.position(), sz = win.size();
        var w = Math.round(Math.max(320, Math.min(screen.width, sz[0] * sc)));
        var h = Math.round(Math.max(220, Math.min(screen.height - 25, sz[1] * sc)));
        win.size = [w, h];
        win.position = [Math.round(Math.max(0, p[0] - (w - sz[0]) / 2)), Math.round(Math.max(25, p[1] - (h - sz[1]) / 2))];
      } catch (e) { win = null; say('error ' + e.message); }
    }
    if (move && win) {
      try {
        win.position = [Math.round(start[0] + parseFloat(move[1]) * screen.width),
                        Math.round(start[1] + parseFloat(move[2]) * screen.height)];
      } catch (e) { win = null; say('error ' + e.message); }
    }
  }
}
