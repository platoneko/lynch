plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "io.lynch.reader"
    compileSdk = 35
    defaultConfig {
        applicationId = "io.lynch.reader"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        ndk { abiFilters += "arm64-v8a" }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation(project(":tailnet"))
    // Each app packages the aar itself: AGP refuses a local .aar bundled into a library.
    implementation(files("../libs/tsnetlib.aar"))
}
