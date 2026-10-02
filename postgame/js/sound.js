/* Postgame — interface sounds
   Short synthesised cues for the two actions that commit something: following
   a member, and writing a diary entry.

   Synthesised through the Web Audio API rather than shipped as audio files.
   Same reasoning as the generated cover art: three cues as .mp3s would be tens
   of kilobytes and a build step, where the whole of this file is under 3KB and
   needs neither. It also means the cues can be tuned by editing numbers.

   Nothing here ever plays without a click behind it. */
window.PG = window.PG || {};
(function (PG) {
  'use strict';

  var KEY = 'postgame.sound';

  /* Note, offset from the start of the cue, and how long it rings — all in
     seconds. Kept short: these punctuate an action, they do not announce it. */
  var VOICES = {
    /* Two rising notes, D5 up to A5. Reads as "yes, done". */
    follow: {
      type: 'triangle', gain: 0.10,
      notes: [[587.33, 0, 0.09], [880.00, 0.065, 0.15]]
    },
    /* The same shape inverted and quieter — undoing something should not sound
       like an achievement. */
    unfollow: {
      type: 'triangle', gain: 0.065,
      notes: [[587.33, 0, 0.09], [392.00, 0.065, 0.14]]
    },
    /* A C major arpeggio, staggered so it lands as one warm chord. Longer than
       the follow cue because saving a diary entry is the bigger commitment. */
    log: {
      type: 'sine', gain: 0.09,
      notes: [[523.25, 0, 0.20], [659.25, 0.055, 0.22], [783.99, 0.11, 0.30]]
    }
  };

  var ctx = null;
  var enabled = read();

  function read() {
    try {
      return window.localStorage.getItem(KEY) !== 'off';
    } catch (e) {
      return true;                    /* storage blocked — default to audible */
    }
  }

  /* Built on first use, never on load: browsers refuse to start an AudioContext
     outside a user gesture, and one created early just sits suspended. */
  function context() {
    if (ctx) return ctx;
    var Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    try {
      ctx = new Ctor();
    } catch (e) {
      return null;                    /* no audio on this device; carry on */
    }
    return ctx;
  }

  function play(name) {
    if (!enabled) return;
    var voice = VOICES[name];
    if (!voice) return;
    var ac = context();
    if (!ac) return;
    /* A context can be suspended by the browser between interactions. */
    if (ac.state === 'suspended' && ac.resume) ac.resume();

    var t0 = ac.currentTime;
    voice.notes.forEach(function (note) {
      var osc = ac.createOscillator();
      var amp = ac.createGain();
      var start = t0 + note[1];
      var end = start + note[2];

      osc.type = voice.type;
      osc.frequency.setValueAtTime(note[0], start);

      /* Ramped in and out rather than switched: a gain that jumps from 0 is an
         instantaneous edge, which speakers reproduce as a click. Exponential
         ramps cannot reach true zero, hence the tiny floor. */
      amp.gain.setValueAtTime(0.0001, start);
      amp.gain.exponentialRampToValueAtTime(voice.gain, start + 0.012);
      amp.gain.exponentialRampToValueAtTime(0.0001, end);

      osc.connect(amp);
      amp.connect(ac.destination);
      osc.start(start);
      osc.stop(end + 0.02);
    });
  }

  function set(on) {
    enabled = !!on;
    try {
      window.localStorage.setItem(KEY, enabled ? 'on' : 'off');
    } catch (e) { /* preference just will not persist */ }
    /* Switching it on demonstrates what you switched on. */
    if (enabled) play('follow');
    return enabled;
  }

  PG.sound = {
    play: play,
    set: set,
    get: function () { return enabled; },
    /* Handy from the console when tuning the cues. */
    voices: VOICES
  };
})(window.PG);
