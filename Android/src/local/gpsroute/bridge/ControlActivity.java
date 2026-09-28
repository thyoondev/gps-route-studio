package local.gpsroute.bridge;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;

/** Shell-only entry point; other apps cannot start a location injection session. */
public final class ControlActivity extends Activity {
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        Intent request = getIntent();
        Intent service = new Intent(this, BridgeService.class);
        service.setAction(request.getStringExtra("action"));
        service.putExtra("port", request.getIntExtra("port", 0));
        service.putExtra("token", request.getStringExtra("token"));
        startForegroundService(service);
        finish();
    }
}
