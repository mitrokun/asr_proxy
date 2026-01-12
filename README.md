* Copy the directory to your configuration folder and restart HA.
* Add `ASR proxy` integration.
* Enter the data for the primary and fallback STT servers.

The first server can be [Speech-to-Phrase](https://github.com/OHF-Voice/speech-to-phrase), which returns an empty value on a miss, serving as a trigger to retry speech recognition on the backup server.

The proxy initially works in streaming mode. When using S2P, you need to enable caching using option `Speech2Phrase mode` in the settings. 

---

This experimental version with extended protocol logic can receive the `TranscriptStop` command from the server. This feature works in tandem with this [asr server](https://github.com/mitrokun/wyoming_streaming_asr).

Example:

_You say "turn on the light," but there's an additional source of noise or speech that your satellite continues to detect and send data to the server.
But the server sees that your command is on its list, stops processing, and passes your command on to the pipeline._

This is not a ready-made solution, but a proof of concept that can and should be improved.
