package cloud.serviciosinformaticos.citas

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.view.View
import android.webkit.*
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import cloud.serviciosinformaticos.citas.databinding.ActivityMainBinding
import java.io.File
import java.text.SimpleDateFormat
import java.util.*

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private var fileUploadCallback: ValueCallback<Array<Uri>>? = null
    private var cameraImageUri: Uri? = null

    private val fileChooserLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (fileUploadCallback == null) return@registerForActivityResult

        var results: Array<Uri>? = null
        if (result.resultCode == Activity.RESULT_OK) {
            val data = result.data
            if (data == null || (data.data == null && data.clipData == null)) {
                cameraImageUri?.let { results = arrayOf(it) }
            } else {
                val dataString = data.dataString
                val clipData = data.clipData
                if (clipData != null) {
                    results = Array(clipData.itemCount) { i -> clipData.getItemAt(i).uri }
                } else if (dataString != null) {
                    results = arrayOf(Uri.parse(dataString))
                }
            }
        }
        fileUploadCallback?.onReceiveValue(results)
        fileUploadCallback = null
    }

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { _ -> }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        requestNecessaryPermissions()
        setupWebView()
        setupSwipeRefresh()
        setupBackNavigation()

        binding.btnRetry.setOnClickListener {
            binding.layoutOffline.visibility = View.GONE
            binding.webView.visibility = View.VISIBLE
            binding.webView.reload()
        }

        binding.webView.loadUrl(BuildConfig.SERVER_URL)
    }

    private fun requestNecessaryPermissions() {
        val permissions = mutableListOf(Manifest.permission.CAMERA)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            permissions.add(Manifest.permission.READ_MEDIA_IMAGES)
        } else {
            permissions.add(Manifest.permission.READ_EXTERNAL_STORAGE)
        }
        val notGranted = permissions.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (notGranted.isNotEmpty()) {
            permissionLauncher.launch(notGranted.toTypedArray())
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(binding.webView, true)

        binding.webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            allowFileAccess = true
            allowContentAccess = true
            loadsImagesAutomatically = true
            mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
            cacheMode = WebSettings.LOAD_DEFAULT
            setSupportZoom(false)
            userAgentString = "$userAgentString NosotrosAndroidApp/1.0"
        }

        binding.webView.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
                super.onPageStarted(view, url, favicon)
                binding.progressBar.visibility = View.VISIBLE
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                super.onPageFinished(view, url)
                binding.progressBar.visibility = View.GONE
                binding.swipeRefreshLayout.isRefreshing = false
                CookieManager.getInstance().flush()
            }

            override fun onReceivedError(
                view: WebView?,
                request: WebResourceRequest?,
                error: WebResourceError?
            ) {
                if (request?.isForMainFrame == true) {
                    binding.swipeRefreshLayout.isRefreshing = false
                    binding.progressBar.visibility = View.GONE
                    binding.webView.visibility = View.GONE
                    binding.layoutOffline.visibility = View.VISIBLE
                }
            }
        }

        binding.webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                webView: WebView?,
                filePathCallback: ValueCallback<Array<Uri>>?,
                fileChooserParams: FileChooserParams?
            ): Boolean {
                fileUploadCallback?.onReceiveValue(null)
                fileUploadCallback = filePathCallback

                val takePictureIntent = Intent(MediaStore.ACTION_IMAGE_CAPTURE)
                var photoFile: File? = null
                try {
                    val timeStamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.getDefault()).format(Date())
                    val storageDir = getExternalFilesDir(Environment.DIRECTORY_PICTURES) ?: cacheDir
                    photoFile = File.createTempFile("NOSOTROS_${timeStamp}_", ".jpg", storageDir)
                } catch (_: Exception) {
                }

                val intentList = mutableListOf<Intent>()
                if (photoFile != null) {
                    cameraImageUri = FileProvider.getUriForFile(
                        this@MainActivity,
                        "${applicationContext.packageName}.fileprovider",
                        photoFile
                    )
                    takePictureIntent.putExtra(MediaStore.EXTRA_OUTPUT, cameraImageUri)
                    takePictureIntent.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
                    intentList.add(takePictureIntent)
                }

                val contentSelectionIntent = Intent(Intent.ACTION_GET_CONTENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    type = "image/*"
                }

                val chooserIntent = Intent(Intent.ACTION_CHOOSER).apply {
                    putExtra(Intent.EXTRA_INTENT, contentSelectionIntent)
                    putExtra(Intent.EXTRA_TITLE, "Elegir o tomar foto del recuerdo")
                    if (intentList.isNotEmpty()) {
                        putExtra(Intent.EXTRA_INITIAL_INTENTS, intentList.toTypedArray())
                    }
                }

                fileChooserLauncher.launch(chooserIntent)
                return true
            }
        }
    }

    private fun setupSwipeRefresh() {
        binding.swipeRefreshLayout.setColorSchemeColors(
            ContextCompat.getColor(this, R.color.primary)
        )
        binding.swipeRefreshLayout.setProgressBackgroundColorSchemeColor(
            ContextCompat.getColor(this, R.color.card_bg)
        )
        binding.swipeRefreshLayout.setOnRefreshListener {
            binding.webView.reload()
        }
        binding.webView.setOnScrollChangeListener { _, _, scrollY, _, _ ->
            binding.swipeRefreshLayout.isEnabled = (scrollY == 0)
        }
    }

    private fun setupBackNavigation() {
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                binding.webView.evaluateJavascript(
                    """
                    (function() {
                      var openDlg = document.querySelector('dialog[open]');
                      if (openDlg) { openDlg.close(); return 'closed_dialog'; }
                      return 'none';
                    })();
                    """.trimIndent()
                ) { result ->
                    if (result != null && result.contains("closed_dialog")) {
                        return@evaluateJavascript
                    }
                    if (binding.webView.canGoBack()) {
                        binding.webView.goBack()
                    } else {
                        isEnabled = false
                        onBackPressedDispatcher.onBackPressed()
                    }
                }
            }
        })
    }
}
