#include "core/Log.hpp"
#include "importer/BfresImporter.hpp"
#include "importer/ImportConfig.hpp"
#include "texture/TextureContainers.hpp"
#include "texture/TextureExporter.hpp"

#include <assimp/Importer.hpp>
#include <assimp/material.h>
#include <assimp/scene.h>

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <exception>
#include <memory>
#include <span>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#if defined(_WIN32)
#define OOTR_EXPORT extern "C" __declspec(dllexport)
#else
#define OOTR_EXPORT extern "C" __attribute__((visibility("default")))
#endif

namespace {

constexpr uint32_t kBridgeVersion = 1;
constexpr size_t kMaxLayers = 8;

struct NativeMesh {
    std::string name;
    uint32_t materialIndex = 0;
    std::vector<float> positions;
    std::vector<float> normals;
    std::vector<float> tangents;
    std::array<std::vector<float>, kMaxLayers> uvs;
    std::array<std::vector<float>, kMaxLayers> colors;
    std::vector<uint32_t> indices;
    std::vector<uint32_t> faceSizes;
};

struct NativeScene {
    std::vector<std::string> materials;
    std::vector<uint8_t> twoSided;
    std::vector<NativeMesh> meshes;
};

struct NativeImage {
    std::string name;
    uint32_t width = 0;
    uint32_t height = 0;
    std::vector<uint8_t> png;
};

thread_local std::string lastError;

void setError(const std::exception& error, const char** output) {
    lastError = error.what();
    if (output != nullptr) {
        *output = lastError.c_str();
    }
}

aiVector3D toBlender(const aiVector3D& value) {
    return {value.x, -value.z, value.y};
}

float dot(const aiVector3D& left, const aiVector3D& right) {
    return left.x * right.x + left.y * right.y + left.z * right.z;
}

void appendVector(std::vector<float>& output, const aiVector3D& value) {
    output.push_back(value.x);
    output.push_back(value.y);
    output.push_back(value.z);
}

std::string uniqueName(
    std::string name,
    std::unordered_map<std::string, unsigned>& counts
) {
    if (name.empty()) {
        name = "Mesh";
    }
    unsigned& count = counts[name];
    ++count;
    return count == 1 ? name : name + "." + std::to_string(count - 1);
}

NativeMesh convertMesh(
    const aiMesh& source,
    const aiMatrix4x4& world,
    std::string name
) {
    NativeMesh mesh;
    mesh.name = std::move(name);
    mesh.materialIndex = source.mMaterialIndex;
    mesh.positions.reserve(size_t(source.mNumVertices) * 3);

    aiMatrix3x3 normalMatrix(world);
    normalMatrix.Inverse().Transpose();
    const bool hasNormals = source.HasNormals();
    const bool hasTangents = source.HasTangentsAndBitangents() && hasNormals;
    if (hasNormals) {
        mesh.normals.reserve(size_t(source.mNumVertices) * 3);
    }
    if (hasTangents) {
        mesh.tangents.reserve(size_t(source.mNumVertices) * 4);
    }

    for (unsigned index = 0; index < source.mNumVertices; ++index) {
        appendVector(mesh.positions, toBlender(world * source.mVertices[index]));
        aiVector3D normal;
        if (hasNormals) {
            normal = toBlender((normalMatrix * source.mNormals[index]).Normalize());
            appendVector(mesh.normals, normal);
        }
        if (hasTangents) {
            const aiVector3D tangent =
                toBlender((normalMatrix * source.mTangents[index]).Normalize());
            const aiVector3D bitangent =
                toBlender((normalMatrix * source.mBitangents[index]).Normalize());
            const float sign = dot(normal ^ tangent, bitangent) < 0.0f ? -1.0f : 1.0f;
            appendVector(mesh.tangents, tangent);
            mesh.tangents.push_back(sign);
        }
    }

    for (size_t layer = 0; layer < kMaxLayers; ++layer) {
        if (source.mTextureCoords[layer] != nullptr) {
            auto& output = mesh.uvs[layer];
            output.reserve(size_t(source.mNumVertices) * 2);
            for (unsigned vertex = 0; vertex < source.mNumVertices; ++vertex) {
                output.push_back(source.mTextureCoords[layer][vertex].x);
                output.push_back(source.mTextureCoords[layer][vertex].y);
            }
        }
        if (source.mColors[layer] != nullptr) {
            auto& output = mesh.colors[layer];
            output.reserve(size_t(source.mNumVertices) * 4);
            for (unsigned vertex = 0; vertex < source.mNumVertices; ++vertex) {
                const aiColor4D& color = source.mColors[layer][vertex];
                output.insert(output.end(), {color.r, color.g, color.b, color.a});
            }
        }
    }

    size_t indexCount = 0;
    for (unsigned face = 0; face < source.mNumFaces; ++face) {
        indexCount += source.mFaces[face].mNumIndices;
    }
    mesh.indices.reserve(indexCount);
    mesh.faceSizes.reserve(source.mNumFaces);
    for (unsigned face = 0; face < source.mNumFaces; ++face) {
        const aiFace& sourceFace = source.mFaces[face];
        if (sourceFace.mNumIndices == 0) {
            continue;
        }
        mesh.faceSizes.push_back(sourceFace.mNumIndices);
        mesh.indices.insert(
            mesh.indices.end(),
            sourceFace.mIndices,
            sourceFace.mIndices + sourceFace.mNumIndices
        );
    }
    for (auto& layer : mesh.uvs) {
        if (layer.empty()) {
            continue;
        }
        std::vector<float> loopValues;
        loopValues.reserve(mesh.indices.size() * 2);
        for (const uint32_t vertex : mesh.indices) {
            loopValues.push_back(layer[size_t(vertex) * 2]);
            loopValues.push_back(layer[size_t(vertex) * 2 + 1]);
        }
        layer = std::move(loopValues);
    }
    return mesh;
}

void collectMeshes(
    const aiScene& source,
    const aiNode& node,
    const aiMatrix4x4& parent,
    NativeScene& output,
    std::unordered_map<std::string, unsigned>& names
) {
    const aiMatrix4x4 world = parent * node.mTransformation;
    for (unsigned index = 0; index < node.mNumMeshes; ++index) {
        const unsigned meshIndex = node.mMeshes[index];
        if (meshIndex >= source.mNumMeshes || source.mMeshes[meshIndex] == nullptr) {
            continue;
        }
        const aiMesh& mesh = *source.mMeshes[meshIndex];
        const std::string baseName =
            mesh.mName.length > 0 ? mesh.mName.C_Str() : node.mName.C_Str();
        output.meshes.push_back(
            convertMesh(mesh, world, uniqueName(baseName, names))
        );
    }
    for (unsigned index = 0; index < node.mNumChildren; ++index) {
        if (node.mChildren[index] != nullptr) {
            collectMeshes(source, *node.mChildren[index], world, output, names);
        }
    }
}

std::unique_ptr<NativeScene> loadScene(
    const uint8_t* data,
    size_t size,
    const char* modelName,
    bool importLods
) {
    if (data == nullptr || size == 0) {
        throw std::runtime_error("BFRES input is empty");
    }
    Assimp::Importer importer;
    bfrass::importer::registerImporter(importer);
    if (modelName != nullptr && *modelName != '\0') {
        importer.SetPropertyString(bfrass::importer::keys::Models, modelName);
    }
    importer.SetPropertyString(bfrass::importer::keys::Rigging, "none");
    importer.SetPropertyString(bfrass::importer::keys::UvLayers, "merge");
    importer.SetPropertyBool(bfrass::importer::keys::ImportLods, importLods);
    importer.SetPropertyBool(bfrass::importer::keys::VertexColors, true);
    importer.SetPropertyBool(bfrass::importer::keys::EmbedTextures, false);
    importer.SetPropertyBool(bfrass::importer::keys::ExportTextures, false);
    importer.SetPropertyBool(bfrass::importer::keys::AutoTextureSiblings, false);
    importer.SetPropertyPointer(
        bfrass::importer::keys::LogPointer,
        const_cast<bfrass::Log*>(&bfrass::Log::null())
    );

    const aiScene* source = importer.ReadFileFromMemory(data, size, 0, "bfres");
    if (source == nullptr) {
        throw std::runtime_error(importer.GetErrorString());
    }
    if (source->mRootNode == nullptr || source->mNumMeshes == 0) {
        throw std::runtime_error("BFRES model contains no importable meshes");
    }

    auto output = std::make_unique<NativeScene>();
    output->materials.reserve(source->mNumMaterials);
    output->twoSided.reserve(source->mNumMaterials);
    for (unsigned index = 0; index < source->mNumMaterials; ++index) {
        aiString name;
        int twoSided = 0;
        source->mMaterials[index]->Get(AI_MATKEY_NAME, name);
        source->mMaterials[index]->Get(AI_MATKEY_TWOSIDED, twoSided);
        output->materials.emplace_back(name.C_Str());
        output->twoSided.push_back(twoSided != 0 ? 1 : 0);
    }

    std::unordered_map<std::string, unsigned> names;
    collectMeshes(
        *source,
        *source->mRootNode,
        aiMatrix4x4(),
        *output,
        names
    );
    if (output->meshes.empty()) {
        throw std::runtime_error("BFRES model produced no mesh instances");
    }
    return output;
}

std::unique_ptr<NativeImage> decodeBntx(
    const uint8_t* data,
    size_t size,
    const char* requestedName
) {
    if (data == nullptr || size == 0) {
        throw std::runtime_error("BNTX input is empty");
    }
    auto owner = std::make_shared<const std::vector<uint8_t>>(data, data + size);
    std::vector<bfrass::tex::TextureResource> textures = bfrass::tex::parseBntx(
        owner,
        std::span<const uint8_t>(*owner),
        "memory.bntx",
        bfrass::Log::null()
    );
    if (textures.empty()) {
        throw std::runtime_error("BNTX archive contains no textures");
    }

    const bfrass::tex::TextureResource* selected = nullptr;
    if (requestedName != nullptr && *requestedName != '\0') {
        const auto match = std::find_if(
            textures.begin(),
            textures.end(),
            [requestedName](const auto& texture) {
                return texture.name == requestedName;
            }
        );
        if (match != textures.end()) {
            selected = &*match;
        }
    }
    if (selected == nullptr && textures.size() == 1) {
        selected = &textures.front();
    }
    if (selected == nullptr) {
        throw std::runtime_error(
            "requested texture is not present in the BNTX archive"
        );
    }

    bfrass::tex::ExportOptions options;
    options.format = bfrass::tex::ExportFormat::Png;
    bfrass::tex::EncodedImage encoded =
        bfrass::tex::encodeTexture(*selected, options);
    auto output = std::make_unique<NativeImage>();
    output->name = selected->name;
    output->width = selected->width;
    output->height = selected->height;
    output->png = std::move(encoded.bytes);
    return output;
}

} // namespace

struct OotrMeshView {
    const char* name;
    uint32_t materialIndex;
    uint32_t vertexCount;
    uint32_t indexCount;
    uint32_t faceCount;
    uint32_t uvLayerCount;
    uint32_t colorLayerCount;
    const float* positions;
    const float* normals;
    const float* tangents;
    const uint32_t* indices;
    const uint32_t* faceSizes;
    const float* uvs[kMaxLayers];
    const float* colors[kMaxLayers];
};

OOTR_EXPORT uint32_t ootr_bridge_version() {
    return kBridgeVersion;
}

OOTR_EXPORT void* ootr_bfres_load(
    const uint8_t* data,
    size_t size,
    const char* modelName,
    int importLods,
    const char** error
) {
    try {
        if (error != nullptr) {
            *error = nullptr;
        }
        return loadScene(data, size, modelName, importLods != 0).release();
    } catch (const std::exception& exception) {
        setError(exception, error);
        return nullptr;
    }
}

OOTR_EXPORT void ootr_scene_free(void* handle) {
    delete static_cast<NativeScene*>(handle);
}

OOTR_EXPORT uint32_t ootr_scene_mesh_count(const void* handle) {
    const auto* scene = static_cast<const NativeScene*>(handle);
    return scene == nullptr ? 0 : static_cast<uint32_t>(scene->meshes.size());
}

OOTR_EXPORT uint32_t ootr_scene_material_count(const void* handle) {
    const auto* scene = static_cast<const NativeScene*>(handle);
    return scene == nullptr ? 0 : static_cast<uint32_t>(scene->materials.size());
}

OOTR_EXPORT const char* ootr_scene_material_name(
    const void* handle,
    uint32_t index
) {
    const auto* scene = static_cast<const NativeScene*>(handle);
    if (scene == nullptr || index >= scene->materials.size()) {
        return nullptr;
    }
    return scene->materials[index].c_str();
}

OOTR_EXPORT int ootr_scene_material_two_sided(
    const void* handle,
    uint32_t index
) {
    const auto* scene = static_cast<const NativeScene*>(handle);
    if (scene == nullptr || index >= scene->twoSided.size()) {
        return 0;
    }
    return scene->twoSided[index] != 0 ? 1 : 0;
}

OOTR_EXPORT int ootr_scene_mesh(
    const void* handle,
    uint32_t index,
    OotrMeshView* output
) {
    const auto* scene = static_cast<const NativeScene*>(handle);
    if (scene == nullptr || output == nullptr || index >= scene->meshes.size()) {
        return 0;
    }
    std::memset(output, 0, sizeof(*output));
    const NativeMesh& mesh = scene->meshes[index];
    output->name = mesh.name.c_str();
    output->materialIndex = mesh.materialIndex;
    output->vertexCount = static_cast<uint32_t>(mesh.positions.size() / 3);
    output->indexCount = static_cast<uint32_t>(mesh.indices.size());
    output->faceCount = static_cast<uint32_t>(mesh.faceSizes.size());
    output->positions = mesh.positions.data();
    output->normals = mesh.normals.empty() ? nullptr : mesh.normals.data();
    output->tangents = mesh.tangents.empty() ? nullptr : mesh.tangents.data();
    output->indices = mesh.indices.data();
    output->faceSizes = mesh.faceSizes.data();
    for (size_t layer = 0; layer < kMaxLayers; ++layer) {
        if (!mesh.uvs[layer].empty()) {
            output->uvs[output->uvLayerCount++] = mesh.uvs[layer].data();
        }
        if (!mesh.colors[layer].empty()) {
            output->colors[output->colorLayerCount++] = mesh.colors[layer].data();
        }
    }
    return 1;
}

OOTR_EXPORT void* ootr_bntx_decode_png(
    const uint8_t* data,
    size_t size,
    const char* textureName,
    const char** error
) {
    try {
        if (error != nullptr) {
            *error = nullptr;
        }
        return decodeBntx(data, size, textureName).release();
    } catch (const std::exception& exception) {
        setError(exception, error);
        return nullptr;
    }
}

OOTR_EXPORT void ootr_image_free(void* handle) {
    delete static_cast<NativeImage*>(handle);
}

OOTR_EXPORT const char* ootr_image_name(const void* handle) {
    const auto* image = static_cast<const NativeImage*>(handle);
    return image == nullptr ? nullptr : image->name.c_str();
}

OOTR_EXPORT uint32_t ootr_image_width(const void* handle) {
    const auto* image = static_cast<const NativeImage*>(handle);
    return image == nullptr ? 0 : image->width;
}

OOTR_EXPORT uint32_t ootr_image_height(const void* handle) {
    const auto* image = static_cast<const NativeImage*>(handle);
    return image == nullptr ? 0 : image->height;
}

OOTR_EXPORT const uint8_t* ootr_image_png_data(const void* handle) {
    const auto* image = static_cast<const NativeImage*>(handle);
    return image == nullptr || image->png.empty() ? nullptr : image->png.data();
}

OOTR_EXPORT size_t ootr_image_png_size(const void* handle) {
    const auto* image = static_cast<const NativeImage*>(handle);
    return image == nullptr ? 0 : image->png.size();
}
