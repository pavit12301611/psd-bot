// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "psd_ai-mlx-image-bridge",
    platforms: [.macOS(.v26)],
    products: [
        .executable(name: "psd_ai-mlx-inpaint", targets: ["PsdAiMLXInpaint"]),
        .executable(name: "psd_ai-mlx-colorize", targets: ["PsdAiMLXColorize"]),
    ],
    dependencies: [
        .package(url: "https://github.com/xocialize/mlx-lama-swift", branch: "main"),
        .package(url: "https://github.com/xocialize/mlx-ddcolor-swift", branch: "main"),
    ],
    targets: [
        .executableTarget(
            name: "PsdAiMLXInpaint",
            dependencies: [
                .product(name: "LaMa", package: "mlx-lama-swift"),
                .product(name: "MIGAN", package: "mlx-lama-swift"),
            ]
        ),
        .executableTarget(
            name: "PsdAiMLXColorize",
            dependencies: [
                .product(name: "DDColor", package: "mlx-ddcolor-swift"),
            ]
        ),
    ]
)
